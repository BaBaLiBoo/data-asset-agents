from __future__ import annotations

from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.models import SQLAsset, TemplateCompatibility
from data_asset_agents.text2sql.models import JoinPlan, SemanticQuery


class TemplateCompatibilityChecker:
    """Prove that a retrieved template covers the complete semantic request."""

    def __init__(self, ontology: OntologyService) -> None:
        self.ontology = ontology

    @staticmethod
    def _join_key(condition: str) -> frozenset[str]:
        return frozenset(
            part.strip().lower() for part in condition.split("=") if part.strip()
        )

    def check(
        self,
        asset: SQLAsset,
        question: str,
        semantic_query: SemanticQuery,
        selected_tables: list[str],
        selected_columns: dict[str, list[str]],
        join_plan: JoinPlan,
    ) -> TemplateCompatibility:
        reasons: list[str] = []
        missing_metrics = sorted(set(semantic_query.metric_ids) - set(asset.metrics))
        if missing_metrics:
            reasons.append("缺少指标：" + ", ".join(missing_metrics))
        missing_dimensions = sorted(
            set(semantic_query.dimension_ids) - set(asset.dimensions)
        )
        if missing_dimensions:
            reasons.append("缺少维度：" + ", ".join(missing_dimensions))
        unexpected_dimensions = sorted(
            set(asset.dimensions) - set(semantic_query.dimension_ids)
        )
        if unexpected_dimensions:
            reasons.append("模板包含未请求维度：" + ", ".join(unexpected_dimensions))
        required_tables = set(selected_tables)
        asset_tables = set(asset.tables)
        if required_tables != asset_tables:
            reasons.append(
                "物理表集合不一致：需要 "
                + ", ".join(sorted(required_tables))
                + "；模板为 "
                + ", ".join(sorted(asset_tables))
            )
        asset_columns = {
            (column.table, column.column)
            for column in asset.columns
            if column.table is not None
        }
        for table, columns in selected_columns.items():
            missing = sorted(
                column for column in columns if (table, column) not in asset_columns
            )
            if missing:
                reasons.append(f"{table} 缺少字段：" + ", ".join(missing))
        required_joins = {self._join_key(step.condition) for step in join_plan.steps}
        asset_joins = {self._join_key(join.expression) for join in asset.joins}
        if required_joins != asset_joins:
            reasons.append("模板 Join 与审核 Join Plan 不一致")
        lowered_question = question.lower()
        window_requested = any(
            term in lowered_question
            for term in ("排名", "排行", "累计", "累积", "rank", "running total")
        )
        subquery_requested = (
            any(term in lowered_question for term in ("高于", "低于", "超过"))
            and any(term in lowered_question for term in ("平均", "均值", "average"))
        )
        required_tags = {"select"}
        if semantic_query.intent == "aggregate":
            required_tags.add("aggregate")
        if semantic_query.dimension_ids:
            required_tags.add("group_by")
        if window_requested:
            required_tags.add("window")
        if subquery_requested:
            required_tags.add("subquery")
        missing_tags = sorted(required_tags - set(asset.structural_tags))
        if missing_tags:
            reasons.append("缺少结构标签：" + ", ".join(missing_tags))
        if "window" in asset.structural_tags and not window_requested:
            reasons.append("模板包含问题未要求的窗口分析结构")
        if "subquery" in asset.structural_tags and not subquery_requested:
            reasons.append("模板包含问题未要求的子查询筛选结构")
        if "subquery" in asset.structural_tags and (
            semantic_query.filters or semantic_query.time_range.kind != "none"
        ):
            reasons.append("带额外筛选或时间范围的子查询模板暂不支持安全改写")
        required_mapping_ids = {
            *(f"metric:{item}" for item in semantic_query.metric_ids),
            *(f"dimension:{item}" for item in semantic_query.dimension_ids),
        }
        missing_mappings = sorted(
            required_mapping_ids - set(asset.physical_mapping_ids)
        )
        if missing_mappings:
            reasons.append("缺少当前版本物理角色映射：" + ", ".join(missing_mappings))
        if asset.ontology_version_id != self.ontology.ontology_version_id:
            reasons.append("模板不属于当前本体版本")
        if not asset.semantic_policy_valid:
            reasons.append("模板未通过业务语义口径校验")
        return TemplateCompatibility(
            asset_id=asset.id,
            compatible=not reasons,
            reasons=reasons,
        )
