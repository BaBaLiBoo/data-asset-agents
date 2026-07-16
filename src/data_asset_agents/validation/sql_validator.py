from collections.abc import Iterable

import sqlglot
from sqlglot import exp

from data_asset_agents.ontology.models import OntologyBundle, TableAsset
from data_asset_agents.text2sql.models import (
    QueryFilter,
    ValidationIssue,
    ValidationReport,
)


class SQLValidator:
    """Enforce syntactic, read-only, lifecycle, schema, join, and metric policies."""

    PROHIBITED = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Alter,
        exp.TruncateTable,
        exp.Create,
        exp.Command,
        exp.Merge,
        exp.Into,
    )
    DENIED_STATUSES = {"DEPRECATED", "TEMPORARY", "TEST"}

    def __init__(self, ontology: OntologyBundle | None = None) -> None:
        self.ontology = ontology
        self.assets = {asset.name: asset for asset in ontology.tables} if ontology else {}
        self.approved_joins = (
            {
                frozenset(
                    (
                        (join.left_table, join.left_column),
                        (join.right_table, join.right_column),
                    )
                )
                for join in ontology.joins
                if join.enabled
            }
            if ontology
            else set()
        )

    @staticmethod
    def _add_issue(
        issues: list[ValidationIssue],
        code: str,
        message: str,
        *,
        table: str | None = None,
        column: str | None = None,
        expected_value: str | None = None,
    ) -> None:
        issue = ValidationIssue(
            code=code,
            message=message,
            table=table,
            column=column,
            expected_value=expected_value,
        )
        if issue not in issues:
            issues.append(issue)

    @staticmethod
    def _table_aliases(statement: exp.Expression) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            aliases[table.name] = table.name
            aliases[table.alias_or_name] = table.name
        return aliases

    @staticmethod
    def _column_reference(
        column: exp.Column,
        aliases: dict[str, str],
        tables: set[str],
        assets: dict[str, TableAsset],
    ) -> tuple[str | None, str]:
        if column.table:
            return aliases.get(column.table), column.name
        candidates = [
            table
            for table in tables
            if table in assets and column.name in assets[table].columns
        ]
        return (candidates[0] if len(candidates) == 1 else None), column.name

    def _validate_columns(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        tables: set[str],
        issues: list[ValidationIssue],
    ) -> None:
        if not self.assets:
            return
        derived_aliases = {
            cte.alias_or_name for cte in statement.find_all(exp.CTE)
        } | {
            subquery.alias_or_name
            for subquery in statement.find_all(exp.Subquery)
            if subquery.alias_or_name
        }
        derived_columns = {
            item.alias_or_name
            for query in [*statement.find_all(exp.CTE), *statement.find_all(exp.Subquery)]
            for select in query.find_all(exp.Select)
            for item in select.expressions
            if item.alias_or_name
        } | {alias.alias for alias in statement.find_all(exp.Alias) if alias.alias}
        for column in statement.find_all(exp.Column):
            if column.table in derived_aliases or (
                not column.table and column.name in derived_columns
            ):
                continue
            table, column_name = self._column_reference(
                column, aliases, tables, self.assets
            )
            if column.table and table is None:
                self._add_issue(
                    issues,
                    "UNKNOWN_TABLE_ALIAS",
                    f"Unknown table alias for column: {column.sql()}",
                    column=column_name,
                )
            elif table is None:
                self._add_issue(
                    issues,
                    "UNKNOWN_OR_AMBIGUOUS_COLUMN",
                    f"Column is unknown or ambiguous: {column_name}",
                    column=column_name,
                )
            elif column_name not in self.assets[table].columns:
                self._add_issue(
                    issues,
                    "UNKNOWN_COLUMN",
                    f"Column {column_name} does not belong to table {table}",
                    table=table,
                    column=column_name,
                )

    def _validate_joins(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        tables: set[str],
        issues: list[ValidationIssue],
    ) -> None:
        if not self.ontology:
            return
        for join in statement.find_all(exp.Join):
            on_expression = join.args.get("on")
            approved = False
            if on_expression is not None:
                for equality in on_expression.find_all(exp.EQ):
                    left = equality.left
                    right = equality.right
                    if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                        continue
                    left_ref = self._column_reference(left, aliases, tables, self.assets)
                    right_ref = self._column_reference(right, aliases, tables, self.assets)
                    if None not in (left_ref[0], right_ref[0]) and frozenset(
                        (left_ref, right_ref)
                    ) in self.approved_joins:
                        approved = True
                        break
            if not approved:
                self._add_issue(
                    issues,
                    "UNAPPROVED_JOIN",
                    f"Join condition is not present in the reviewed Join Graph: {join.sql()}",
                )

    def _validate_required_filters(
        self,
        statement: exp.Expression,
        aliases: dict[str, str],
        tables: set[str],
        required_filters: Iterable[QueryFilter],
        issues: list[ValidationIssue],
    ) -> None:
        actual: set[tuple[str | None, str, str]] = set()
        for where in statement.find_all(exp.Where):
            for equality in where.find_all(exp.EQ):
                pairs = ((equality.left, equality.right), (equality.right, equality.left))
                for possible_column, possible_value in pairs:
                    if isinstance(possible_column, exp.Column) and isinstance(
                        possible_value, (exp.Literal, exp.Boolean)
                    ):
                        table, column = self._column_reference(
                            possible_column, aliases, tables, self.assets
                        )
                        actual.add((table, column, str(possible_value.this)))
        for required in required_filters:
            expected = (required.table, required.field, required.value)
            if expected not in actual:
                self._add_issue(
                    issues,
                    "MISSING_REQUIRED_FILTER",
                    f"Missing required metric filter: {required.table}.{required.field} = "
                    f"{required.value}",
                    table=required.table,
                    column=required.field,
                    expected_value=required.value,
                )

    def validate(
        self,
        sql: str,
        allowed_tables: set[str] | None = None,
        required_filters: Iterable[QueryFilter] = (),
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            issue = ValidationIssue(code="PARSE_ERROR", message=f"SQL parse error: {exc}")
            return ValidationReport(valid=False, errors=[issue.message], issues=[issue])
        if len(statements) != 1:
            self._add_issue(
                issues,
                "MULTIPLE_STATEMENTS",
                "Exactly one SQL statement is allowed",
            )
        statement = statements[0] if statements else None
        if statement is None:
            issue = ValidationIssue(code="EMPTY_SQL", message="SQL is empty")
            return ValidationReport(valid=False, errors=[issue.message], issues=[issue])
        is_query = isinstance(statement, exp.Query)
        if not is_query:
            self._add_issue(issues, "NOT_SELECT", "Only SELECT queries are allowed")
        prohibited = [
            node.key.upper()
            for node in statement.walk()
            if isinstance(node, self.PROHIBITED)
        ]
        if prohibited:
            self._add_issue(
                issues,
                "PROHIBITED_OPERATION",
                f"Prohibited SQL operation(s): {', '.join(sorted(set(prohibited)))}",
            )

        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        table_names = {
            table.name
            for table in statement.find_all(exp.Table)
            if table.name not in cte_names
        }
        aliases = self._table_aliases(statement)
        if allowed_tables is not None:
            unexpected = table_names - allowed_tables
            if unexpected:
                self._add_issue(
                    issues,
                    "NON_SELECTED_TABLE",
                    f"SQL references non-selected table(s): {', '.join(sorted(unexpected))}",
                )
        for table in sorted(table_names):
            asset = self.assets.get(table)
            if self.ontology and asset is None:
                self._add_issue(
                    issues,
                    "UNKNOWN_TABLE",
                    f"Table is not defined in the reviewed ontology: {table}",
                    table=table,
                )
            elif asset and (asset.status in self.DENIED_STATUSES or not asset.selectable):
                self._add_issue(
                    issues,
                    "FORBIDDEN_LIFECYCLE_TABLE",
                    f"Table {table} is {asset.status} and cannot be selected",
                    table=table,
                )

        self._validate_columns(statement, aliases, table_names, issues)
        self._validate_joins(statement, aliases, table_names, issues)
        self._validate_required_filters(
            statement,
            aliases,
            table_names,
            required_filters,
            issues,
        )
        read_only = is_query and not prohibited
        return ValidationReport(
            valid=not issues,
            read_only=read_only,
            statement_type=statement.key.upper(),
            tables=sorted(table_names),
            errors=[issue.message for issue in issues],
            issues=issues,
            formatted_sql=statement.sql(dialect="postgres", pretty=True),
        )
