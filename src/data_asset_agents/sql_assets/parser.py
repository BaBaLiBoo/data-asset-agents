from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    ColumnReference,
    HistoricalSQLAnalysis,
    HistoricalSQLSummary,
    ParsedAggregate,
    ParsedJoin,
    UsageCount,
)
from data_asset_agents.sql_assets.models import (
    CertificationLevel,
    SQLAsset,
    SQLExecutionStatus,
)

if TYPE_CHECKING:
    from data_asset_agents.ontology.service import OntologyService


def _split_conjunction(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.And):
        return [
            *_split_conjunction(expression.left),
            *_split_conjunction(expression.right),
        ]
    return [expression]


class HistoricalSQLParser:
    """Parse certified historical SQL into deterministic structural evidence."""

    def parse(
        self,
        sql: str,
        *,
        question: str | None = None,
        certified: bool = False,
    ) -> HistoricalSQLAnalysis:
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise OntologyError(f"Historical SQL parse error: {exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise OntologyError("Historical SQL must contain exactly one read-only query")
        statement = statements[0]
        aliases: dict[str, str] = {}
        tables: list[str] = []
        for table in statement.find_all(exp.Table):
            aliases[table.alias_or_name] = table.name
            aliases[table.name] = table.name
            if table.name not in tables:
                tables.append(table.name)

        columns: list[ColumnReference] = []
        seen_columns: set[tuple[str | None, str]] = set()
        for column in statement.find_all(exp.Column):
            table_name = (
                aliases.get(column.table)
                if column.table
                else tables[0]
                if len(tables) == 1
                else None
            )
            key = (table_name, column.name)
            if key not in seen_columns:
                seen_columns.add(key)
                columns.append(ColumnReference(table=table_name, column=column.name))

        joins: list[ParsedJoin] = []
        for join in statement.find_all(exp.Join):
            on_expression = join.args.get("on")
            if on_expression is None:
                continue
            for equality in on_expression.find_all(exp.EQ):
                left = equality.left
                right = equality.right
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                left_table = aliases.get(left.table, left.table)
                right_table = aliases.get(right.table, right.table)
                if not left_table or not right_table:
                    continue
                joins.append(
                    ParsedJoin(
                        left_table=left_table,
                        left_column=left.name,
                        right_table=right_table,
                        right_column=right.name,
                        expression=(
                            f"{left_table}.{left.name} = "
                            f"{right_table}.{right.name}"
                        ),
                    )
                )

        where = statement.args.get("where")
        filters = (
            [item.sql(dialect="postgres") for item in _split_conjunction(where.this)]
            if where is not None
            else []
        )
        aggregates: list[ParsedAggregate] = []
        for aggregate in statement.find_all(exp.AggFunc):
            parent = aggregate.parent
            alias = parent.alias if isinstance(parent, exp.Alias) else None
            aggregates.append(
                ParsedAggregate(
                    function=aggregate.key.upper(),
                    expression=aggregate.sql(dialect="postgres"),
                    alias=alias or None,
                )
            )
        group = statement.args.get("group")
        group_by = (
            [item.sql(dialect="postgres") for item in group.expressions]
            if group is not None
            else []
        )
        time_fields = [
            column
            for column in columns
            if any(
                token in column.column.lower()
                for token in ("date", "time", "month", "year")
            )
        ]

        return HistoricalSQLAnalysis(
            question=question,
            sql_text=sql,
            certified=certified,
            tables=tables,
            columns=columns,
            joins=joins,
            filters=filters,
            aggregates=aggregates,
            group_by=group_by,
            time_fields=time_fields,
        )

    def parse_file(self, path: Path | str) -> list[HistoricalSQLAnalysis]:
        source_path = Path(path)
        try:
            payload: list[dict[str, Any]] = json.loads(
                source_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise OntologyError(
                f"Cannot load historical SQL file {source_path}: {exc}"
            ) from exc
        return [
            self.parse(
                str(item["sql"]),
                question=item.get("question"),
                certified=bool(item.get("certified", False)),
            )
            for item in payload
        ]

    def parse_assets(
        self,
        path: Path | str,
        ontology: OntologyService,
    ) -> list[SQLAsset]:
        """Parse a JSON batch and enrich it against the published ontology."""

        source_path = Path(path)
        try:
            payload: list[dict[str, Any]] = json.loads(
                source_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise OntologyError(
                f"Cannot load historical SQL file {source_path}: {exc}"
            ) from exc
        return [self.parse_asset(item, ontology) for item in payload]

    def parse_asset(
        self,
        record: dict[str, Any],
        ontology: OntologyService,
    ) -> SQLAsset:
        """Create one SQLAsset; parse failures are stored as ineligible evidence."""

        sql = str(record.get("sql") or record.get("sql_text") or "")
        question = str(record.get("question") or "")
        stable_id = str(record.get("id") or self._asset_id(question, sql))
        certified = bool(record.get("certified", False))
        level = record.get(
            "certification_level",
            CertificationLevel.CERTIFIED if certified else CertificationLevel.NONE,
        )
        common: dict[str, Any] = {
            "id": stable_id,
            "question": question,
            "business_summary": str(record.get("business_summary") or question),
            "certified": certified,
            "certification_level": level,
            "sql_text": sql,
            "dialect": str(record.get("dialect") or "postgres"),
        }
        try:
            analysis = self.parse(sql, question=question, certified=certified)
            statement = sqlglot.parse_one(sql, read=common["dialect"])
        except (OntologyError, sqlglot.errors.ParseError) as exc:
            return SQLAsset(
                **common,
                parse_error=str(exc),
                execution_status=SQLExecutionStatus.PARSE_FAILED,
                updated_at=datetime.now(UTC),
            )

        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        physical_tables = [table for table in analysis.tables if table not in cte_names]
        columns = [
            column
            for column in analysis.columns
            if column.table is None or column.table not in cte_names
        ]
        joins = [
            join
            for join in analysis.joins
            if join.left_table not in cte_names and join.right_table not in cte_names
        ]
        select_aliases = {
            alias.alias for alias in statement.find_all(exp.Alias) if alias.alias
        }
        invalid_columns = self._invalid_columns(
            columns, physical_tables, ontology, select_aliases
        )
        unapproved = self._unapproved_joins(joins, ontology)
        lifecycle_valid = all(
            (asset := ontology.tables_by_name.get(table)) is not None
            and asset.status == "ACTIVE"
            and asset.selectable
            for table in physical_tables
        )
        order = statement.args.get("order")
        limit_expression = statement.args.get("limit")
        limit_value: int | None = None
        if limit_expression is not None and isinstance(limit_expression.expression, exp.Literal):
            with suppress(TypeError, ValueError):
                limit_value = int(limit_expression.expression.this)
        ctes = [cte.alias_or_name for cte in statement.find_all(exp.CTE)]
        windows = [node.sql(dialect="postgres") for node in statement.find_all(exp.Window)]
        aggregates = [node.sql(dialect="postgres") for node in statement.find_all(exp.AggFunc)]
        node_types = Counter(type(node).__name__ for node in statement.walk())
        tags = self._structural_tags(statement, analysis, ctes, windows)
        metrics = self._matched_metrics(statement, physical_tables, ontology)
        dimensions = self._matched_dimensions(columns, ontology)
        mappings = self._matched_mappings(physical_tables, columns, ontology)
        normalized = self._normalized_sql(statement)
        return SQLAsset(
            **common,
            metrics=metrics,
            dimensions=dimensions,
            filters=analysis.filters,
            tables=physical_tables,
            columns=columns,
            joins=joins,
            group_by=analysis.group_by,
            order_by=(
                [item.sql(dialect="postgres") for item in order.expressions]
                if order is not None
                else []
            ),
            limit=limit_value,
            ctes=ctes,
            subquery_count=sum(1 for _ in statement.find_all(exp.Subquery)),
            window_functions=windows,
            aggregates=aggregates,
            ast_node_types=dict(sorted(node_types.items())),
            ast_fingerprint=hashlib.sha256(normalized.encode()).hexdigest(),
            structural_tags=tags,
            physical_mapping_ids=mappings,
            invalid_columns=invalid_columns,
            unapproved_joins=unapproved,
            lifecycle_valid=lifecycle_valid,
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _asset_id(question: str, sql: str) -> str:
        digest = hashlib.sha256(f"{question}\0{sql}".encode()).hexdigest()[:24]
        return f"sqlasset-{digest}"

    @staticmethod
    def _normalized_sql(statement: exp.Expression) -> str:
        normalized = statement.copy()
        for literal in list(normalized.find_all(exp.Literal)):
            replacement = exp.Literal.string("?") if literal.is_string else exp.Literal.number("0")
            literal.replace(replacement)
        return normalized.sql(dialect="postgres", normalize=True, pretty=False)

    @staticmethod
    def _aliases(statement: exp.Expression, cte_names: set[str]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            if table.name in cte_names:
                continue
            aliases[table.name] = table.name
            aliases[table.alias_or_name] = table.name
        return aliases

    @staticmethod
    def _invalid_columns(
        columns: list[ColumnReference],
        tables: list[str],
        ontology: OntologyService,
        select_aliases: set[str] | None = None,
    ) -> list[str]:
        invalid: list[str] = []
        for column in columns:
            if not column.table and column.column in (select_aliases or set()):
                continue
            if column.table:
                asset = ontology.tables_by_name.get(column.table)
                if asset is None or column.column not in asset.columns:
                    invalid.append(f"{column.table}.{column.column}")
            elif not any(
                column.column in ontology.tables_by_name[table].columns
                for table in tables
                if table in ontology.tables_by_name
            ):
                invalid.append(column.column)
        return sorted(set(invalid))

    @staticmethod
    def _unapproved_joins(
        joins: list[ParsedJoin], ontology: OntologyService
    ) -> list[str]:
        approved = {
            frozenset(
                (
                    (join.left_table, join.left_column),
                    (join.right_table, join.right_column),
                )
            )
            for join in ontology.bundle.joins
            if join.enabled
        }
        return [
            join.expression
            for join in joins
            if frozenset(
                (
                    (join.left_table, join.left_column),
                    (join.right_table, join.right_column),
                )
            )
            not in approved
        ]

    @staticmethod
    def _matched_metrics(
        statement: exp.Expression,
        tables: list[str],
        ontology: OntologyService,
    ) -> list[str]:
        aggregate_sql = " ".join(
            node.sql(dialect="postgres", normalize=True)
            for node in statement.find_all(exp.AggFunc)
        ).lower()
        matched: list[str] = []
        for metric in ontology.metrics_by_id.values():
            if metric.base_table not in tables:
                continue
            try:
                metric_ast = sqlglot.parse_one(metric.expression, read="postgres")
                required_functions = {
                    node.key for node in metric_ast.find_all(exp.AggFunc)
                }
                required_columns = {
                    node.name for node in metric_ast.find_all(exp.Column)
                }
            except sqlglot.errors.ParseError:
                continue
            asset_columns = {node.name for node in statement.find_all(exp.Column)}
            if required_columns <= asset_columns and all(
                function.lower() in aggregate_sql for function in required_functions
            ):
                matched.append(metric.id)
        return sorted(set(matched))

    @staticmethod
    def _matched_dimensions(
        columns: list[ColumnReference], ontology: OntologyService
    ) -> list[str]:
        references = {(item.table, item.column) for item in columns}
        return sorted(
            dimension.id
            for dimension in ontology.dimensions_by_id.values()
            if (dimension.table, dimension.column) in references
        )

    @staticmethod
    def _matched_mappings(
        tables: list[str],
        columns: list[ColumnReference],
        ontology: OntologyService,
    ) -> list[str]:
        references = {(item.table, item.column) for item in columns}
        return sorted(
            mapping.concept_id
            for mapping in ontology.bundle.mappings
            if mapping.table in tables
            and any((mapping.table, column) in references for column in mapping.columns)
        )

    @staticmethod
    def _structural_tags(
        statement: exp.Expression,
        analysis: HistoricalSQLAnalysis,
        ctes: list[str],
        windows: list[str],
    ) -> list[str]:
        tags = ["select"]
        if analysis.joins:
            tags.append("join")
        if analysis.aggregates:
            tags.append("aggregate")
        if analysis.group_by:
            tags.append("group_by")
        if statement.args.get("order") is not None:
            tags.append("order_by")
        if statement.args.get("limit") is not None:
            tags.append("limit")
        if ctes:
            tags.append("cte")
        if any(True for _ in statement.find_all(exp.Subquery)):
            tags.append("subquery")
        if windows:
            tags.append("window")
        return tags

    def summarize(
        self, analyses: list[HistoricalSQLAnalysis]
    ) -> HistoricalSQLSummary:
        table_pairs: Counter[str] = Counter()
        column_usage: Counter[str] = Counter()
        join_usage: Counter[str] = Counter()
        for analysis in analyses:
            for left, right in combinations(sorted(set(analysis.tables)), 2):
                table_pairs[f"{left}|{right}"] += 1
            for column in analysis.columns:
                key = f"{column.table}.{column.column}" if column.table else column.column
                column_usage[key] += 1
            for join in analysis.joins:
                endpoints = sorted(
                    [
                        f"{join.left_table}.{join.left_column}",
                        f"{join.right_table}.{join.right_column}",
                    ]
                )
                join_usage[" = ".join(endpoints)] += 1
        return HistoricalSQLSummary(
            parsed_count=len(analyses),
            table_cooccurrence=self._usage_counts(table_pairs),
            column_usage=self._usage_counts(column_usage),
            join_usage=self._usage_counts(join_usage),
        )

    @staticmethod
    def _usage_counts(counter: Counter[str]) -> list[UsageCount]:
        return [
            UsageCount(key=key, count=count)
            for key, count in sorted(
                counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]
