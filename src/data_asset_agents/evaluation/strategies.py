from __future__ import annotations

import re
from contextlib import suppress
from time import perf_counter
from typing import Any, Protocol

import sqlglot

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError, QueryExecutionError
from data_asset_agents.evaluation.models import StrategyResult, TokenUsage
from data_asset_agents.evaluation.physical_rag import PhysicalRAGIndex
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.text2sql.models import TraceStep, ValidationReport
from data_asset_agents.validation import CommonSQLSafetyValidator, DatabaseCatalog


class QueryStrategy(Protocol):
    def execute(self, question: str) -> StrategyResult: ...


def _trace(node: str, summary: str, status: str = "completed") -> TraceStep:
    return TraceStep(node=node, summary=summary, status=status)  # type: ignore[arg-type]


def _extract_sql(raw: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    return (fenced.group(1) if fenced else raw).strip().rstrip(";")


def _references(sql: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    statement = sqlglot.parse_one(sql, read="postgres")
    ctes = {cte.alias_or_name for cte in statement.find_all(sqlglot.exp.CTE)}
    aliases = {table.alias_or_name: table.name for table in statement.find_all(sqlglot.exp.Table)}
    tables = sorted(
        {table.name for table in statement.find_all(sqlglot.exp.Table) if table.name not in ctes}
    )
    columns: dict[str, list[str]] = {}
    for column in statement.find_all(sqlglot.exp.Column):
        table = aliases.get(column.table) if column.table else None
        if table in tables:
            columns.setdefault(table, []).append(column.name)
    columns = {table: sorted(set(items)) for table, items in columns.items()}
    joins = [
        on.sql(dialect="postgres")
        for join in statement.find_all(sqlglot.exp.Join)
        if (on := join.args.get("on")) is not None
    ]
    return tables, columns, joins


class _PhysicalStrategy:
    def __init__(
        self,
        catalog: DatabaseCatalog,
        executor: ExecutorProtocol,
        settings: Settings,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.catalog = catalog
        self.executor = executor
        self.settings = settings
        self.model_factory = model_factory or ModelFactory(settings)
        self.common_validator = CommonSQLSafetyValidator(catalog)

    def _run_sql(
        self,
        *,
        question: str,
        mode: str,
        variant: str,
        sql: str | None,
        raw: str | None,
        context: list[dict[str, Any]] | None,
        started: float,
        token_usage: TokenUsage | None = None,
    ) -> StrategyResult:
        if sql is None:
            return StrategyResult(
                question=question,
                query_mode=mode,  # type: ignore[arg-type]
                strategy_variant=variant,  # type: ignore[arg-type]
                status="unsupported",
                error_code="UNSUPPORTED_QUERY",
                unsupported_reason="The physical baseline could not map the question to SQL",
                raw_model_output=raw,
                retrieved_context=context,
                latency_ms=round((perf_counter() - started) * 1000, 2),
                token_usage=token_usage,
                trace_steps=[_trace("generate_sql", "No physical SQL candidate", "failed")],
                mode_metadata={"run_kind": "smoke" if self.settings.llm_mode == "mock" else "live"},
            )
        common = self.common_validator.validate(sql)
        tables: list[str] = []
        columns: dict[str, list[str]] = {}
        joins: list[str] = []
        if common.formatted_sql:
            with suppress(sqlglot.errors.ParseError):
                tables, columns, joins = _references(sql)
        execution = None
        status = "failed"
        if common.valid:
            try:
                execution = self.executor.execute(sql, set(tables))
                common = common.model_copy(update={"explain_passed": True})
                status = "success"
            except QueryExecutionError as exc:
                common = common.model_copy(
                    update={
                        "valid": False,
                        "explain_passed": False,
                        "errors": [*common.errors, str(exc)],
                    }
                )
        return StrategyResult(
            question=question,
            query_mode=mode,  # type: ignore[arg-type]
            strategy_variant=variant,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            generated_sql=sql,
            raw_model_output=raw,
            retrieved_context=context,
            selected_tables=tables,
            selected_columns=columns,
            discovered_joins=joins,
            semantic_query=None,
            join_plan=None,
            sql_asset_candidates=None,
            selected_sql_asset=None,
            sql_rewrite=None,
            common_validation_report=common,
            ontology_policy_report=None,
            execution_result=execution,
            latency_ms=round((perf_counter() - started) * 1000, 2),
            token_usage=token_usage,
            trace_steps=[
                _trace("generate_sql", f"{variant} produced SQL"),
                _trace(
                    "common_validation",
                    "Common safety passed" if common.valid else "; ".join(common.errors),
                    "completed" if common.valid else "failed",
                ),
            ],
            mode_metadata={"run_kind": "smoke" if self.settings.llm_mode == "mock" else "live"},
        )

    def _live_sql(self, prompt: str) -> tuple[str, str, TokenUsage]:
        response = self.model_factory.chat_model().invoke(prompt)
        raw = str(response.content)
        usage = getattr(response, "usage_metadata", None) or {}
        token_usage = TokenUsage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
        )
        return _extract_sql(raw), raw, token_usage


class SchemaBaselineStrategy(_PhysicalStrategy):
    """Schema-only baseline; its constructor cannot receive OntologyService."""

    def execute(self, question: str) -> StrategyResult:
        started = perf_counter()
        if self.settings.llm_mode == "mock":
            sql = self._mock_sql(question)
            return self._run_sql(
                question=question,
                mode="schema",
                variant="schema",
                sql=sql,
                raw=sql,
                context=None,
                started=started,
            )
        prompt = (
            "Generate one PostgreSQL SELECT query. Return SQL only. You may use only "
            "the raw schema below; it contains no business semantics.\n\n"
            + self.catalog.schema_prompt()
            + "\n\nQUESTION:\n"
            + question
        )
        sql, raw, tokens = self._live_sql(prompt)
        return self._run_sql(
            question=question,
            mode="schema",
            variant="schema",
            sql=sql,
            raw=raw,
            context=None,
            started=started,
            token_usage=tokens,
        )

    @staticmethod
    def _mock_sql(question: str) -> str | None:
        if any(term in question for term in ("天气", "气温", "股票")):
            return None
        if "交易" not in question and "消费" not in question:
            return None
        # Smoke-only deterministic baseline intentionally uses the most literal
        # physical amount column and has no hidden ontology policy assistance.
        amount = "SUM(t.txn_amount) AS transaction_amount"
        count = "COUNT(DISTINCT t.transaction_id) AS transaction_count"
        select_items = [amount]
        if "笔数" in question:
            select_items.append(count)
        if "分行" in question or "机构" in question:
            return (
                "SELECT b.branch_name, "
                + ", ".join(select_items)
                + " FROM dwd_card_transaction t JOIN dim_branch b "
                "ON t.branch_id = b.branch_id "
                "WHERE t.transaction_date >= CURRENT_DATE - INTERVAL '30 days' "
                "GROUP BY b.branch_name ORDER BY b.branch_name"
            )
        return "SELECT " + ", ".join(select_items) + " FROM dwd_card_transaction t"


class PhysicalRAGStrategy(_PhysicalStrategy):
    """Physical metadata/raw SQL RAG with no ontology or SQLAsset dependency."""

    def __init__(
        self,
        catalog: DatabaseCatalog,
        index: PhysicalRAGIndex,
        executor: ExecutorProtocol,
        settings: Settings,
        model_factory: ModelFactory | None = None,
    ) -> None:
        super().__init__(catalog, executor, settings, model_factory)
        self.index = index

    def execute(self, question: str) -> StrategyResult:
        started = perf_counter()
        retrieved = self.index.search(question, limit=8)
        context = [item.model_dump(mode="json") for item in retrieved]
        if self.settings.llm_mode == "mock":
            sql = next(
                (
                    item.document.raw_sql
                    for item in retrieved
                    if item.document.document_type == "historical_sql" and item.document.raw_sql
                ),
                SchemaBaselineStrategy._mock_sql(question),
            )
            return self._run_sql(
                question=question,
                mode="rag",
                variant="rag",
                sql=sql,
                raw=sql,
                context=context,
                started=started,
            )
        allowed_context = "\n\n".join(item.document.search_text for item in retrieved)
        prompt = (
            "Generate one PostgreSQL SELECT query from physical metadata and raw historical "
            "SQL only. Return SQL only. Do not assume business rules not present here.\n\n"
            + allowed_context
            + "\n\nQUESTION:\n"
            + question
        )
        sql, raw, tokens = self._live_sql(prompt)
        return self._run_sql(
            question=question,
            mode="rag",
            variant="rag",
            sql=sql,
            raw=raw,
            context=context,
            started=started,
            token_usage=tokens,
        )


class OntologyStrategy:
    def __init__(self, graph: Any, *, sql_asset_enabled: bool) -> None:
        self.graph = graph
        self.sql_asset_enabled = sql_asset_enabled

    def execute(self, question: str) -> StrategyResult:
        started = perf_counter()
        result = self.graph.invoke(
            {
                "question": question,
                "query_mode": "ontology",
                "retry_count": 0,
                "trace_steps": [],
            }
        )
        validation: ValidationReport | None = result.get("validation_report")
        return StrategyResult(
            question=question,
            query_mode="ontology",
            strategy_variant=(
                "ontology_full" if self.sql_asset_enabled else "ontology_no_sql_asset"
            ),
            status=result.get("status", "failed"),
            error_code=result.get("error_code"),
            unsupported_reason=result.get("unsupported_reason"),
            generated_sql=result.get("generated_sql"),
            raw_model_output=None,
            retrieved_context=(
                [item.model_dump(mode="json") for item in result.get("matched_concepts", [])]
                if result.get("matched_concepts")
                else []
            ),
            selected_tables=result.get("selected_tables", []),
            selected_columns=result.get("selected_columns", {}),
            discovered_joins=[step.condition for step in (result.get("join_plan") or []).steps]
            if result.get("join_plan")
            else [],
            semantic_query=result.get("semantic_query"),
            join_plan=result.get("join_plan"),
            sql_asset_candidates=(
                [item.model_dump(mode="json") for item in result.get("sql_asset_candidates", [])]
                if self.sql_asset_enabled
                else None
            ),
            selected_sql_asset=(
                result["selected_sql_asset"].model_dump(mode="json")
                if self.sql_asset_enabled and result.get("selected_sql_asset")
                else None
            ),
            selected_template_rank=result.get("selected_template_rank"),
            template_rejection_reasons=result.get("template_rejection_reasons", {}),
            sql_rewrite=(
                result["sql_rewrite"].model_dump(mode="json")
                if self.sql_asset_enabled and result.get("sql_rewrite")
                else None
            ),
            common_validation_report=result.get("common_validation_report", validation),
            ontology_policy_report=result.get("ontology_policy_report", validation),
            execution_result=result.get("execution_result"),
            latency_ms=round((perf_counter() - started) * 1000, 2),
            token_usage=None,
            trace_steps=result.get("trace_steps", []),
            mode_metadata={
                "run_kind": "smoke",
                "sql_asset_enabled": self.sql_asset_enabled,
            },
        )


class StrategyRouter:
    def __init__(self, strategies: dict[str, QueryStrategy]) -> None:
        self.strategies = strategies

    def execute(
        self,
        question: str,
        query_mode: str,
        *,
        sql_asset_enabled: bool = True,
    ) -> StrategyResult:
        key = (
            "ontology_full"
            if query_mode == "ontology" and sql_asset_enabled
            else ("ontology_no_sql_asset" if query_mode == "ontology" else query_mode)
        )
        strategy = self.strategies.get(key)
        if strategy is None:
            raise DataAssetAgentsError(f"Query strategy is not configured: {key}")
        return strategy.execute(question)
