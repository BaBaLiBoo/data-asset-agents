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
        certification_level: str | None = None,
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

        filters = [
            item.sql(dialect="postgres")
            for where in statement.find_all(exp.Where)
            for item in _split_conjunction(where.this)
        ]
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
        group_by = [
            item.sql(dialect="postgres")
            for group in statement.find_all(exp.Group)
            for item in group.expressions
        ]
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
            certification_level=certification_level
            or ("CERTIFIED" if certified else "NONE"),
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
                certification_level=str(item.get("certification_level") or "NONE"),
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
        inferred_metrics = self._matched_metrics(statement, physical_tables, ontology)
        inferred_dimensions = self._matched_dimensions(columns, ontology)
        declared_metrics = [str(item) for item in record.get("metrics", [])]
        declared_dimensions = [str(item) for item in record.get("dimensions", [])]
        metrics = declared_metrics or inferred_metrics
        dimensions = declared_dimensions or inferred_dimensions
        mappings = self._matched_mappings(physical_tables, columns, ontology)
        policy_violations = self._validate_semantic_policy(
            statement,
            metrics,
            dimensions,
            mappings,
            ontology,
        )
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
            semantic_policy_valid=not policy_violations,
            metric_policy_violations=policy_violations,
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
            if column.column in (select_aliases or set()):
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

    @classmethod
    def _validate_semantic_policy(
        cls,
        statement: exp.Expression,
        metric_ids: list[str],
        dimension_ids: list[str],
        mapping_ids: list[str],
        ontology: OntologyService,
    ) -> list[str]:
        """Validate that certified SQL implements the published business contract."""

        violations: list[str] = []
        aliases = {
            alias: table.name
            for table in statement.find_all(exp.Table)
            for alias in {table.name, table.alias_or_name}
        }
        physical_tables = set(aliases.values())

        def resolve_table(column: exp.Column) -> str | None:
            if column.table:
                return aliases.get(column.table)
            candidates = [
                table
                for table in physical_tables
                if table in ontology.tables_by_name
                and column.name in ontology.tables_by_name[table].columns
            ]
            return candidates[0] if len(candidates) == 1 else None

        actual_filters: dict[tuple[str | None, str], set[str]] = {}
        for equality in statement.find_all(exp.EQ):
            for possible_column, possible_value in (
                (equality.left, equality.right),
                (equality.right, equality.left),
            ):
                if isinstance(possible_column, exp.Column) and isinstance(
                    possible_value, (exp.Literal, exp.Boolean)
                ):
                    key = (resolve_table(possible_column), possible_column.name)
                    actual_filters.setdefault(key, set()).add(
                        str(possible_value.this).upper()
                    )
        for predicate in statement.find_all(exp.In):
            if not isinstance(predicate.this, exp.Column):
                continue
            key = (resolve_table(predicate.this), predicate.this.name)
            values = {
                str(item.this).upper()
                for item in predicate.expressions
                if isinstance(item, exp.Literal)
            }
            if values:
                actual_filters.setdefault(key, set()).update(values)
        for (table, column), values in actual_filters.items():
            if len(values) > 1 and any(
                isinstance(parent, exp.EQ)
                for node in statement.find_all(exp.Column)
                if resolve_table(node) == table and node.name == column
                for parent in [node.parent]
            ):
                violations.append(
                    f"CONFLICTING_FILTER:{table or '?'}.{column}="
                    + "|".join(sorted(values))
                )

        actual_aggregates = list(statement.find_all(exp.AggFunc))
        time_filter_refs: set[tuple[str | None, str]] = set()
        for predicate_type in (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
            for predicate in statement.find_all(predicate_type):
                for column in predicate.find_all(exp.Column):
                    if any(
                        token in column.name.lower()
                        for token in ("date", "time", "month", "year")
                    ):
                        time_filter_refs.add((resolve_table(column), column.name))

        for metric_id in metric_ids:
            metric = ontology.metrics_by_id.get(metric_id)
            if metric is None:
                violations.append(f"UNKNOWN_METRIC:{metric_id}")
                continue
            mapping_id = f"metric:{metric_id}"
            mapping = ontology.mappings_by_concept_id.get(mapping_id)
            if mapping is None or mapping_id not in mapping_ids:
                violations.append(f"metric:{metric_id}:MAPPING_NOT_IN_CURRENT_VERSION")
                continue
            if mapping.table != metric.base_table:
                violations.append(f"metric:{metric_id}:MAPPING_TABLE_MISMATCH")
            for field, expected in metric.required_filters.items():
                values = actual_filters.get((metric.base_table, field), set())
                if expected.upper() not in values:
                    violations.append(
                        f"metric:{metric_id}:MISSING_REQUIRED_FILTER:{field}={expected}"
                    )

            try:
                expected_expression = sqlglot.parse_one(
                    metric.expression, read="postgres"
                )
            except sqlglot.errors.ParseError:
                violations.append(f"metric:{metric_id}:INVALID_METRIC_EXPRESSION")
                continue
            expected_aggregates = list(expected_expression.find_all(exp.AggFunc))
            for expected_aggregate in expected_aggregates:
                expected_columns = {
                    (column.table or metric.base_table, column.name)
                    for column in expected_aggregate.find_all(exp.Column)
                }
                expected_distinct = expected_aggregate.find(exp.Distinct) is not None
                matched = False
                for actual in actual_aggregates:
                    actual_columns = {
                        (aliases.get(column.table) or metric.base_table, column.name)
                        for column in actual.find_all(exp.Column)
                    }
                    if (
                        actual.key == expected_aggregate.key
                        and (actual.find(exp.Distinct) is not None) == expected_distinct
                        and expected_columns <= actual_columns
                    ):
                        matched = True
                        break
                if not matched:
                    violations.append(
                        f"metric:{metric_id}:AGGREGATION_OR_ROLE_MISMATCH:"
                        f"{expected_aggregate.sql(dialect='postgres')}"
                    )

            if metric.time_dimension:
                time_dimension = ontology.dimensions_by_id.get(metric.time_dimension)
                if time_dimension is None:
                    violations.append(f"metric:{metric_id}:UNKNOWN_TIME_DIMENSION")
                elif time_filter_refs and time_filter_refs != {
                    (time_dimension.table, time_dimension.column)
                }:
                    rendered = ",".join(
                        f"{table or '?'}.{column}"
                        for table, column in sorted(time_filter_refs, key=str)
                    )
                    violations.append(
                        f"metric:{metric_id}:TIME_FIELD_MISMATCH:{rendered}"
                    )
            unsupported = sorted(set(dimension_ids) - set(metric.supported_dimensions))
            if unsupported:
                violations.append(
                    f"metric:{metric_id}:UNSUPPORTED_DIMENSION:"
                    + ",".join(unsupported)
                )
        for dimension_id in dimension_ids:
            if dimension_id not in ontology.dimensions_by_id:
                violations.append(f"UNKNOWN_DIMENSION:{dimension_id}")
            elif f"dimension:{dimension_id}" not in mapping_ids:
                violations.append(
                    f"dimension:{dimension_id}:MAPPING_NOT_IN_CURRENT_VERSION"
                )
        return list(dict.fromkeys(violations))

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
