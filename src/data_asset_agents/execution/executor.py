from collections.abc import Sequence
from time import perf_counter
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import QueryExecutionError
from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.text2sql.models import ExecutionResult
from data_asset_agents.validation import CommonSQLSafetyValidator, DatabaseCatalog


class QueryExecutor:
    """EXPLAIN and execute validated SQL inside a read-only PostgreSQL transaction."""

    def __init__(
        self,
        settings: Settings,
        ontology: OntologyBundle | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine or create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_timeout=settings.database_connect_timeout,
            connect_args={"connect_timeout": settings.database_connect_timeout},
        )
        self.catalog = DatabaseCatalog.from_engine(self.engine)
        self.validator = CommonSQLSafetyValidator(self.catalog)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def set_ontology(self, ontology: OntologyBundle) -> None:
        """Refresh execution validation after an ontology version is published."""

        # Execution safety is based on live database metadata. Ontology business
        # policy is deliberately enforced before this mode-neutral executor.
        self.catalog = DatabaseCatalog.from_engine(self.engine)
        self.validator = CommonSQLSafetyValidator(self.catalog)

    def execute(self, sql: str, allowed_tables: set[str]) -> ExecutionResult:
        report = self.validator.validate(sql, allowed_tables)
        if not report.valid:
            raise QueryExecutionError("; ".join(report.errors))
        started = perf_counter()
        try:
            with self.engine.connect() as connection, connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(
                    text(f"SET LOCAL statement_timeout = {self.settings.sql_statement_timeout_ms}")
                )
                explain_rows: Sequence[Any] = connection.execute(text(f"EXPLAIN {sql}")).fetchall()
                result = connection.execute(text(sql))
                columns = list(result.keys())
                fetched = result.mappings().fetchmany(self.settings.sql_max_rows + 1)
                truncated = len(fetched) > self.settings.sql_max_rows
                fetched = fetched[: self.settings.sql_max_rows]
        except SQLAlchemyError as exc:
            raise QueryExecutionError(f"PostgreSQL validation/execution failed: {exc}") from exc
        return ExecutionResult(
            columns=columns,
            rows=[dict(row) for row in fetched],
            row_count=len(fetched),
            truncated=truncated,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            explain_plan=[str(row[0]) for row in explain_rows],
        )

    def explain(self, sql: str, allowed_tables: set[str]) -> list[str]:
        """Run PostgreSQL EXPLAIN without executing the proposed query."""

        report = self.validator.validate(sql, allowed_tables)
        if not report.valid:
            raise QueryExecutionError("; ".join(report.errors))
        try:
            with self.engine.connect() as connection, connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(
                    text(
                        f"SET LOCAL statement_timeout = "
                        f"{self.settings.sql_statement_timeout_ms}"
                    )
                )
                rows: Sequence[Any] = connection.execute(text(f"EXPLAIN {sql}")).fetchall()
        except SQLAlchemyError as exc:
            raise QueryExecutionError(f"PostgreSQL EXPLAIN failed: {exc}") from exc
        return [str(row[0]) for row in rows]
