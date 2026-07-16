from __future__ import annotations

from collections.abc import Iterable

import sqlglot
from sqlglot import exp

from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.text2sql.models import (
    QueryFilter,
    SemanticQuery,
    ValidationIssue,
    ValidationReport,
)


class OntologyPolicyValidator:
    """Validate business semantics; never used by schema or physical-RAG baselines."""

    DENIED_STATUSES = {"DEPRECATED", "TEMPORARY", "TEST"}

    def __init__(self, ontology: OntologyBundle, version_id: str | None = None) -> None:
        self.ontology = ontology
        self.version_id = version_id
        self.assets = {asset.name: asset for asset in ontology.tables}
        self.approved_joins = {
            frozenset(
                (
                    (join.left_table, join.left_column),
                    (join.right_table, join.right_column),
                )
            )
            for join in ontology.joins
            if join.enabled
        }

    @staticmethod
    def _aliases(statement: exp.Expression) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            aliases[table.alias_or_name] = table.name
            aliases[table.name] = table.name
        return aliases

    @staticmethod
    def _add(
        issues: list[ValidationIssue],
        code: str,
        message: str,
        **details: str | None,
    ) -> None:
        issue = ValidationIssue(code=code, message=message, **details)
        if issue not in issues:
            issues.append(issue)

    def validate(
        self,
        sql: str,
        *,
        semantic_query: SemanticQuery | None = None,
        required_filters: Iterable[QueryFilter] = (),
        expected_version_id: str | None = None,
    ) -> ValidationReport:
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            issue = ValidationIssue(code="PARSE_ERROR", message=f"SQL parse error: {exc}")
            return ValidationReport(valid=False, errors=[issue.message], issues=[issue])
        issues: list[ValidationIssue] = []
        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        tables = {
            table.name
            for table in statement.find_all(exp.Table)
            if table.name not in cte_names
        }
        aliases = self._aliases(statement)
        for table_name in sorted(tables):
            asset = self.assets.get(table_name)
            if asset is None:
                self._add(
                    issues,
                    "UNMAPPED_TABLE",
                    f"Table is absent from the published ontology: {table_name}",
                    table=table_name,
                )
            elif asset.status in self.DENIED_STATUSES or not asset.selectable:
                self._add(
                    issues,
                    "FORBIDDEN_LIFECYCLE_TABLE",
                    f"Table {table_name} is {asset.status} and cannot be selected",
                    table=table_name,
                )
        self._validate_joins(statement, aliases, issues)
        self._validate_filters(statement, aliases, required_filters, issues)
        if semantic_query is not None:
            self._validate_semantic_contract(statement, aliases, semantic_query, issues)
        if (
            expected_version_id
            and self.version_id
            and expected_version_id != self.version_id
        ):
            self._add(
                issues,
                "ONTOLOGY_VERSION_MISMATCH",
                "Query was compiled against a different published ontology version",
            )
        return ValidationReport(
            valid=not issues,
            read_only=True,
            statement_type=statement.key.upper(),
            tables=sorted(tables),
            errors=[issue.message for issue in issues],
            issues=issues,
            formatted_sql=statement.sql(dialect="postgres", pretty=True),
        )

    def _column_ref(
        self, column: exp.Column, aliases: dict[str, str]
    ) -> tuple[str | None, str]:
        return aliases.get(column.table) if column.table else None, column.name

    def _validate_joins(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        issues: list[ValidationIssue],
    ) -> None:
        for join in statement.find_all(exp.Join):
            on_expression = join.args.get("on")
            approved = False
            for equality in on_expression.find_all(exp.EQ) if on_expression else []:
                if isinstance(equality.left, exp.Column) and isinstance(
                    equality.right, exp.Column
                ):
                    edge = frozenset(
                        (
                            self._column_ref(equality.left, aliases),
                            self._column_ref(equality.right, aliases),
                        )
                    )
                    approved = approved or edge in self.approved_joins
            if not approved:
                self._add(
                    issues,
                    "UNAPPROVED_JOIN",
                    f"Join is absent from the reviewed Join Graph: {join.sql()}",
                )

    def _validate_filters(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        required_filters: Iterable[QueryFilter],
        issues: list[ValidationIssue],
    ) -> None:
        actual: set[tuple[str | None, str, str]] = set()
        for where in statement.find_all(exp.Where):
            for equality in where.find_all(exp.EQ):
                for possible_column, possible_value in (
                    (equality.left, equality.right),
                    (equality.right, equality.left),
                ):
                    if isinstance(possible_column, exp.Column) and isinstance(
                        possible_value, (exp.Literal, exp.Boolean)
                    ):
                        actual.add(
                            (
                                aliases.get(possible_column.table)
                                if possible_column.table
                                else None,
                                possible_column.name,
                                str(possible_value.this),
                            )
                        )
        for required in required_filters:
            qualified = (required.table, required.field, required.value)
            unqualified = (None, required.field, required.value)
            if qualified not in actual and unqualified not in actual:
                self._add(
                    issues,
                    "MISSING_REQUIRED_FILTER",
                    f"Missing required metric filter: {required.field} = {required.value}",
                    table=required.table,
                    column=required.field,
                    expected_value=required.value,
                )

    def _validate_semantic_contract(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        semantic: SemanticQuery,
        issues: list[ValidationIssue],
    ) -> None:
        mappings = {mapping.concept_id: mapping for mapping in self.ontology.mappings}
        metrics = {metric.id: metric for metric in self.ontology.metrics}
        for metric_id in semantic.metric_ids:
            metric = metrics.get(metric_id)
            mapping = mappings.get(f"metric:{metric_id}")
            if metric is None or mapping is None:
                self._add(
                    issues,
                    "MISSING_PHYSICAL_MAPPING",
                    f"Metric has no published Physical Mapping: {metric_id}",
                )
                continue
            unsupported = set(semantic.dimension_ids) - set(metric.supported_dimensions)
            for dimension in sorted(unsupported):
                self._add(
                    issues,
                    "UNSUPPORTED_DIMENSION",
                    f"Metric {metric_id} does not support dimension {dimension}",
                )
            aggregate_key = metric.expression.split("(", 1)[0].upper()
            binding_role = (
                "transaction_id"
                if "DISTINCT" in metric.expression.upper()
                else ("customer_id" if "CUSTOMER" in metric.id.upper() else "amount")
            )
            physical_column = mapping.column_bindings.get(binding_role)
            matching_aggregate = False
            for aggregate in statement.find_all(exp.AggFunc):
                if aggregate.key.upper() != aggregate_key:
                    continue
                if physical_column is None or any(
                    column.name == physical_column for column in aggregate.find_all(exp.Column)
                ):
                    matching_aggregate = True
            if not matching_aggregate:
                self._add(
                    issues,
                    "METRIC_AGGREGATION_MISMATCH",
                    f"Metric {metric_id} aggregation or physical role is incorrect",
                )
            if semantic.time_range.kind != "none":
                expected_time = mapping.column_bindings.get("event_time")
                time_columns = {
                    column.name
                    for where in statement.find_all(exp.Where)
                    for column in where.find_all(exp.Column)
                    if "date" in column.name.lower() or "time" in column.name.lower()
                }
                if expected_time and expected_time not in time_columns:
                    self._add(
                        issues,
                        "TIME_DIMENSION_MISMATCH",
                        f"Metric {metric_id} must use time field {expected_time}",
                        column=expected_time,
                    )
