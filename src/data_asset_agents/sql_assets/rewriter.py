from __future__ import annotations

import sqlglot
from sqlglot import exp

from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.ontology.models import PhysicalMapping
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.models import SQLAsset, SQLRewriteResult
from data_asset_agents.text2sql.models import JoinPlan, QueryFilter, SemanticQuery
from data_asset_agents.validation import SQLValidator


class SQLTemplateRewriteError(ValueError):
    pass


class SQLTemplateRewriter:
    """Rewrite certified templates exclusively through SQLGlot AST mutations."""

    def __init__(self, ontology: OntologyService, executor: ExecutorProtocol) -> None:
        self.ontology = ontology
        self.executor = executor
        self.validator = SQLValidator(ontology.bundle)

    @staticmethod
    def _outer_select(statement: exp.Expression) -> exp.Select:
        if isinstance(statement, exp.Select):
            return statement
        select = statement.find(exp.Select)
        if select is None:
            raise SQLTemplateRewriteError("模板没有可改写的 SELECT")
        return select

    @staticmethod
    def _table_map(
        asset: SQLAsset,
        source_mappings: list[PhysicalMapping],
        target_mappings: list[PhysicalMapping],
        join_plan: JoinPlan,
    ) -> dict[str, str]:
        """Map tables only when a semantic role proves the correspondence."""

        targets = list(join_plan.tables)
        if not targets:
            raise SQLTemplateRewriteError("Join Plan 没有目标表")
        target_by_concept = {
            mapping.concept_id: mapping for mapping in target_mappings
        }
        proposed: dict[str, set[str]] = {}
        for source in source_mappings:
            target = target_by_concept.get(source.concept_id)
            if target is not None:
                proposed.setdefault(source.table, set()).add(target.table)
        mapping: dict[str, str] = {}
        for source_table in asset.tables:
            role_targets = proposed.get(source_table, set())
            if len(role_targets) > 1:
                raise SQLTemplateRewriteError(
                    f"模板表 {source_table} 的业务角色映射相互冲突"
                )
            if role_targets:
                mapping[source_table] = next(iter(role_targets))
            elif source_table in targets:
                mapping[source_table] = source_table
            else:
                raise SQLTemplateRewriteError(
                    f"无法依据 Physical Mapping 证明表角色：{source_table}"
                )
        if set(mapping.values()) != set(targets):
            raise SQLTemplateRewriteError("模板表角色未完整覆盖目标 Join Plan")
        return mapping

    @staticmethod
    def _column_map(
        source_mappings: list[PhysicalMapping],
        target_mappings: list[PhysicalMapping],
        table_map: dict[str, str],
    ) -> dict[tuple[str, str], str]:
        targets = {mapping.concept_id: mapping for mapping in target_mappings}
        result: dict[tuple[str, str], str] = {}
        for source in source_mappings:
            target = targets.get(source.concept_id)
            if target is None:
                continue
            if table_map.get(source.table) != target.table:
                continue
            for role, source_column in source.column_bindings.items():
                target_column = target.column_for(role)
                if target_column:
                    result[(source.table, source_column)] = target_column
        return result

    def rewrite_ast(
        self,
        asset: SQLAsset,
        deterministic_sql: str,
        semantic_query: SemanticQuery,
        target_mappings: list[PhysicalMapping],
        join_plan: JoinPlan,
    ) -> tuple[str, list[str]]:
        """Return an AST-rewritten template without validating or executing it."""

        try:
            template = sqlglot.parse_one(asset.sql_text, read=asset.dialect)
            target = sqlglot.parse_one(deterministic_sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise SQLTemplateRewriteError(f"SQLGlot 解析失败：{exc}") from exc
        source_mappings = [
            mapping
            for mapping in self.ontology.bundle.mappings
            if mapping.concept_id in asset.physical_mapping_ids
        ]
        table_map = self._table_map(
            asset, source_mappings, target_mappings, join_plan
        )
        column_map = self._column_map(source_mappings, target_mappings, table_map)
        template_join_keys = {
            frozenset(
                {
                    (table_map.get(join.left_table, join.left_table), join.left_column),
                    (table_map.get(join.right_table, join.right_table), join.right_column),
                }
            )
            for join in asset.joins
        }
        target_join_keys = {
            frozenset(
                {
                    (step.left_table, step.left_column),
                    (step.right_table, step.right_column),
                }
            )
            for step in join_plan.steps
        }
        rebuild_join_tree = template_join_keys != target_join_keys
        aliases: dict[str, str] = {}
        for table in template.find_all(exp.Table):
            aliases[table.alias_or_name] = table.name
            aliases[table.name] = table.name
        changes: list[str] = []
        for table in list(template.find_all(exp.Table)):
            replacement = table_map.get(table.name)
            if replacement and replacement != table.name:
                changes.append(f"表 {table.name} → {replacement}")
                table.set("this", exp.to_identifier(replacement))
        for column in list(template.find_all(exp.Column)):
            source_table = aliases.get(column.table, column.table) if column.table else None
            replacement = column_map.get((source_table, column.name)) if source_table else None
            if replacement and replacement != column.name:
                changes.append(f"字段 {source_table}.{column.name} → {replacement}")
                column.set("this", exp.to_identifier(replacement))
            if column.table and column.table in table_map and not any(
                table.alias == column.table for table in template.find_all(exp.Table)
            ):
                column.set("table", exp.to_identifier(table_map[column.table]))

        template_target_aliases = {
            table.name: table.alias_or_name
            for table in template.find_all(exp.Table)
            if table.name in join_plan.tables
        }
        target_alias_to_table = {
            table.alias_or_name: table.name for table in target.find_all(exp.Table)
        }

        def translated(node: exp.Expression) -> exp.Expression:
            copied = node.copy()
            for column in copied.find_all(exp.Column):
                target_table = target_alias_to_table.get(column.table)
                replacement_alias = template_target_aliases.get(target_table or "")
                if replacement_alias:
                    column.set("table", exp.to_identifier(replacement_alias))
            return copied

        template_outer = self._outer_select(template)
        target_outer = self._outer_select(target)

        def owns_physical_table(select: exp.Select) -> bool:
            for table in select.find_all(exp.Table):
                ancestor = table.parent
                while ancestor is not None and not isinstance(ancestor, exp.Select):
                    ancestor = ancestor.parent
                if ancestor is select and table.name in set(table_map.values()):
                    return True
            return False

        physical_select = next(
            (
                select
                for select in template.find_all(exp.Select)
                if owns_physical_table(select)
            ),
            template_outer,
        )
        if rebuild_join_tree:
            complex_structure = {"cte", "subquery", "window"} & set(
                asset.structural_tags
            )
            if physical_select is not template_outer or complex_structure:
                raise SQLTemplateRewriteError(
                    "模板 Join 与目标 Join Plan 不一致，复杂结构无法安全重建"
                )
            physical_select.set("from_", target_outer.args.get("from_").copy())
            physical_select.set(
                "joins",
                [join.copy() for join in target_outer.args.get("joins", [])],
            )
            changes.append("从确定性 SQL AST 重建 FROM/JOIN 子树")
        target_where = target_outer.args.get("where")
        if target_where is not None:
            physical_select.set("where", translated(target_where))
            changes.append("按 SemanticQuery 重建时间范围和筛选条件")
        target_group = target_outer.args.get("group")
        if target_group is not None:
            physical_select.set("group", translated(target_group))
            changes.append("按目标维度重建 GROUP BY")
        elif physical_select is template_outer:
            physical_select.set("group", None)
        target_order = target_outer.args.get("order")
        if physical_select is template_outer or semantic_query.order_by:
            template_outer.set("order", translated(target_order) if target_order else None)
        if target_order is not None and (
            physical_select is template_outer or semantic_query.order_by
        ):
            changes.append("按 SemanticQuery 重建 ORDER BY")
        target_limit = target_outer.args.get("limit")
        template_outer.set("limit", translated(target_limit) if target_limit else None)
        if target_limit is not None:
            changes.append("按 SemanticQuery 重建 LIMIT")

        if semantic_query.dimension_ids and physical_select is template_outer:
            target_aliases = {item.alias_or_name for item in target_outer.expressions}
            preserved = [
                item.copy()
                for item in template_outer.expressions
                if item.find(exp.Window) is not None and item.alias_or_name not in target_aliases
            ]
            template_outer.set(
                "expressions",
                [translated(item) for item in target_outer.expressions] + preserved,
            )
            changes.append("按目标指标和维度重建 SELECT，并保留窗口表达式")
        return template.sql(dialect="postgres", pretty=True), list(dict.fromkeys(changes))

    def rewrite_or_fallback(
        self,
        asset: SQLAsset,
        deterministic_sql: str,
        semantic_query: SemanticQuery,
        target_mappings: list[PhysicalMapping],
        join_plan: JoinPlan,
        required_filters: list[QueryFilter],
    ) -> SQLRewriteResult:
        """Rewrite, validate and EXPLAIN; return deterministic SQL on any failure."""

        try:
            rewritten, changes = self.rewrite_ast(
                asset,
                deterministic_sql,
                semantic_query,
                target_mappings,
                join_plan,
            )
            report = self.validator.validate(
                rewritten, set(join_plan.tables), required_filters
            )
            if not report.valid:
                raise SQLTemplateRewriteError("；".join(report.errors))
            explain_plan = self.executor.explain(rewritten, set(join_plan.tables))
            return SQLRewriteResult(
                used_template=True,
                asset_id=asset.id,
                original_sql=asset.sql_text,
                rewritten_sql=rewritten,
                changes=changes,
                explain_plan=explain_plan,
            )
        except (SQLTemplateRewriteError, DataAssetAgentsError, ValueError, RuntimeError) as exc:
            return SQLRewriteResult(
                used_template=False,
                asset_id=asset.id,
                original_sql=asset.sql_text,
                rewritten_sql=deterministic_sql,
                fallback_reason=f"AST 模板改写未通过，使用确定性编译器：{exc}",
            )
