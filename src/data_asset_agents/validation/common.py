from __future__ import annotations

from typing import Any

import sqlglot
from pydantic import BaseModel, Field
from sqlalchemy import Engine, inspect
from sqlglot import exp

from data_asset_agents.text2sql.models import ValidationIssue, ValidationReport


class CatalogColumn(BaseModel):
    name: str
    data_type: str
    nullable: bool = True


class CatalogForeignKey(BaseModel):
    columns: list[str] = Field(default_factory=list)
    referred_table: str
    referred_columns: list[str] = Field(default_factory=list)


class CatalogTable(BaseModel):
    schema_name: str = "public"
    name: str
    columns: list[CatalogColumn] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[CatalogForeignKey] = Field(default_factory=list)
    comment: str | None = None

    @property
    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}


class DatabaseCatalog(BaseModel):
    """Pure physical database facts with no ontology or business semantics."""

    schema_name: str = "public"
    tables: list[CatalogTable] = Field(default_factory=list)

    @classmethod
    def from_engine(cls, engine: Engine, schema_name: str = "public") -> DatabaseCatalog:
        inspector = inspect(engine)
        tables: list[CatalogTable] = []
        for table_name in sorted(inspector.get_table_names(schema=schema_name)):
            columns = inspector.get_columns(table_name, schema=schema_name)
            primary_key = inspector.get_pk_constraint(
                table_name, schema=schema_name
            ).get("constrained_columns") or []
            foreign_keys = [
                CatalogForeignKey(
                    columns=list(item.get("constrained_columns") or []),
                    referred_table=str(item.get("referred_table") or ""),
                    referred_columns=list(item.get("referred_columns") or []),
                )
                for item in inspector.get_foreign_keys(table_name, schema=schema_name)
                if item.get("referred_table")
            ]
            try:
                comment = inspector.get_table_comment(
                    table_name, schema=schema_name
                ).get("text")
            except NotImplementedError:
                comment = None
            tables.append(
                CatalogTable(
                    schema_name=schema_name,
                    name=table_name,
                    columns=[
                        CatalogColumn(
                            name=str(column["name"]),
                            data_type=str(column["type"]),
                            nullable=bool(column.get("nullable", True)),
                        )
                        for column in columns
                    ],
                    primary_key=list(primary_key),
                    foreign_keys=foreign_keys,
                    comment=comment,
                )
            )
        return cls(schema_name=schema_name, tables=tables)

    @classmethod
    def from_table_columns(
        cls, table_columns: dict[str, list[str] | set[str]]
    ) -> DatabaseCatalog:
        return cls(
            tables=[
                CatalogTable(
                    name=table,
                    columns=[
                        CatalogColumn(name=column, data_type="UNKNOWN")
                        for column in sorted(columns)
                    ],
                )
                for table, columns in sorted(table_columns.items())
            ]
        )

    @property
    def table_map(self) -> dict[str, CatalogTable]:
        return {table.name: table for table in self.tables}

    def business_only(self) -> DatabaseCatalog:
        """Exclude application control-plane tables without consulting ontology."""

        infrastructure_prefixes = (
            "ontology_",
            "semantic_",
            "candidate_",
            "metadata_",
            "sql_asset",
            "concept_",
            "physical_mapping",
            "review_",
            "evaluation_",
            "physical_rag_",
        )
        return self.model_copy(
            update={
                "tables": [
                    table
                    for table in self.tables
                    if not table.name.startswith(infrastructure_prefixes)
                ]
            }
        )

    def schema_prompt(self) -> str:
        """Render only the fields permitted in the schema-only experiment."""

        lines: list[str] = []
        for table in self.tables:
            lines.append(f"TABLE {table.name}")
            for column in table.columns:
                nullable = "NULL" if column.nullable else "NOT NULL"
                lines.append(f"  {column.name} {column.data_type} {nullable}")
            if table.primary_key:
                lines.append("  PRIMARY KEY (" + ", ".join(table.primary_key) + ")")
            for foreign_key in table.foreign_keys:
                lines.append(
                    "  FOREIGN KEY ("
                    + ", ".join(foreign_key.columns)
                    + ") REFERENCES "
                    + foreign_key.referred_table
                    + " ("
                    + ", ".join(foreign_key.referred_columns)
                    + ")"
                )
        return "\n".join(lines)


class CommonSQLSafetyValidator:
    """Mode-neutral SQL safety checks based only on physical database facts."""

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

    def __init__(
        self,
        catalog: DatabaseCatalog | None = None,
        *,
        allowed_schemas: set[str] | None = None,
        max_sql_length: int = 20_000,
    ) -> None:
        self.catalog = catalog or DatabaseCatalog()
        self.tables = self.catalog.table_map
        self.allowed_schemas = allowed_schemas or {"public"}
        self.max_sql_length = max_sql_length

    @staticmethod
    def _add(
        issues: list[ValidationIssue],
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        issue = ValidationIssue(code=code, message=message, **details)
        if issue not in issues:
            issues.append(issue)

    @staticmethod
    def _aliases(statement: exp.Expression) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            aliases[table.name] = table.name
            aliases[table.alias_or_name] = table.name
        return aliases

    def validate(
        self,
        sql: str,
        allowed_tables: set[str] | None = None,
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        if len(sql) > self.max_sql_length:
            self._add(issues, "SQL_TOO_LONG", "SQL exceeds the configured length limit")
        if "--" in sql or "/*" in sql or "*/" in sql:
            self._add(
                issues,
                "SQL_COMMENT_FORBIDDEN",
                "SQL comments are forbidden to prevent statement smuggling",
            )
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            issue = ValidationIssue(code="PARSE_ERROR", message=f"SQL parse error: {exc}")
            return ValidationReport(valid=False, errors=[issue.message], issues=[issue])
        if len(statements) != 1:
            self._add(issues, "MULTIPLE_STATEMENTS", "Exactly one SQL statement is allowed")
        statement = statements[0] if statements else None
        if statement is None:
            issue = ValidationIssue(code="EMPTY_SQL", message="SQL is empty")
            return ValidationReport(valid=False, errors=[issue.message], issues=[issue])

        is_query = isinstance(statement, exp.Query)
        if not is_query:
            self._add(issues, "NOT_SELECT", "Only read-only SELECT/WITH queries are allowed")
        prohibited = sorted(
            {
                node.key.upper()
                for node in statement.walk()
                if isinstance(node, self.PROHIBITED)
            }
        )
        if prohibited:
            self._add(
                issues,
                "PROHIBITED_OPERATION",
                "Prohibited SQL operation(s): " + ", ".join(prohibited),
            )

        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        physical_tables: set[str] = set()
        for table in statement.find_all(exp.Table):
            if table.name in cte_names:
                continue
            if table.db and table.db not in self.allowed_schemas:
                self._add(
                    issues,
                    "FORBIDDEN_SCHEMA",
                    f"Schema is not allowlisted: {table.db}",
                    table=table.name,
                )
            physical_tables.add(table.name)
        if allowed_tables is not None:
            unexpected = physical_tables - allowed_tables
            if unexpected:
                self._add(
                    issues,
                    "NON_SELECTED_TABLE",
                    "SQL references non-selected table(s): "
                    + ", ".join(sorted(unexpected)),
                )
        if self.tables:
            for table in sorted(physical_tables):
                if table not in self.tables:
                    self._add(
                        issues,
                        "UNKNOWN_TABLE",
                        f"Table does not exist in the controlled database: {table}",
                        table=table,
                    )
            self._validate_columns(statement, physical_tables, cte_names, issues)

        read_only = is_query and not prohibited
        return ValidationReport(
            valid=not issues,
            read_only=read_only,
            statement_type=statement.key.upper(),
            tables=sorted(physical_tables),
            errors=[issue.message for issue in issues],
            issues=issues,
            formatted_sql=statement.sql(dialect="postgres", pretty=True),
        )

    def _validate_columns(
        self,
        statement: exp.Expression,
        physical_tables: set[str],
        cte_names: set[str],
        issues: list[ValidationIssue],
    ) -> None:
        aliases = self._aliases(statement)
        derived_aliases = cte_names | {
            subquery.alias_or_name
            for subquery in statement.find_all(exp.Subquery)
            if subquery.alias_or_name
        }
        derived_columns = {
            item.alias_or_name
            for select in statement.find_all(exp.Select)
            for item in select.expressions
            if item.alias_or_name
        }
        for column in statement.find_all(exp.Column):
            if column.table in derived_aliases:
                continue
            table_name = aliases.get(column.table) if column.table else None
            if table_name:
                table = self.tables.get(table_name)
                if table and column.name not in table.column_names:
                    self._add(
                        issues,
                        "UNKNOWN_COLUMN",
                        f"Column {column.name} does not belong to table {table_name}",
                        table=table_name,
                        column=column.name,
                    )
                continue
            if not column.table and column.name in derived_columns:
                continue
            candidates = [
                table
                for table in physical_tables
                if table in self.tables and column.name in self.tables[table].column_names
            ]
            if not candidates:
                self._add(
                    issues,
                    "UNKNOWN_COLUMN",
                    f"Column does not exist in referenced tables: {column.name}",
                    column=column.name,
                )
            elif len(candidates) > 1:
                self._add(
                    issues,
                    "AMBIGUOUS_COLUMN",
                    f"Column must be qualified because it exists in multiple tables: {column.name}",
                    column=column.name,
                )
