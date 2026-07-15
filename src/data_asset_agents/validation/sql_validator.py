import sqlglot
from sqlglot import exp

from data_asset_agents.text2sql.models import ValidationReport


class SQLValidator:
    """Parse SQL and enforce a single-statement, SELECT-only allowlist policy."""

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
    )

    def validate(self, sql: str, allowed_tables: set[str] | None = None) -> ValidationReport:
        errors: list[str] = []
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            return ValidationReport(valid=False, errors=[f"SQL parse error: {exc}"])
        if len(statements) != 1:
            errors.append("Exactly one SQL statement is allowed")
        statement = statements[0] if statements else None
        if statement is None:
            errors.append("SQL is empty")
            return ValidationReport(valid=False, errors=errors)
        if not isinstance(statement, exp.Query):
            errors.append("Only SELECT queries are allowed")
        prohibited = [node.key.upper() for node in statement.walk() if isinstance(node, self.PROHIBITED)]
        if prohibited:
            errors.append(f"Prohibited SQL operation(s): {', '.join(sorted(set(prohibited)))}")
        tables = sorted({table.name for table in statement.find_all(exp.Table)})
        if allowed_tables is not None:
            unexpected = set(tables) - allowed_tables
            if unexpected:
                errors.append(f"SQL references non-selected table(s): {', '.join(sorted(unexpected))}")
        return ValidationReport(
            valid=not errors,
            read_only=not errors,
            statement_type=statement.key.upper(),
            tables=tables,
            errors=errors,
            formatted_sql=statement.sql(dialect="postgres", pretty=True),
        )

