from operator import add
from typing import Annotated, TypedDict

from data_asset_agents.text2sql.models import (
    ExecutionResult,
    HistoricalSQLExample,
    JoinPlan,
    MatchedConcept,
    RejectedTable,
    SemanticQuery,
    TraceStep,
    ValidationReport,
)


class Text2SQLState(TypedDict, total=False):
    """Stable, typed contract for the standalone Text-to-SQL subgraph."""

    question: str
    query_mode: str
    status: str
    error_code: str | None
    unsupported_reason: str | None
    semantic_query: SemanticQuery
    matched_concepts: list[MatchedConcept]
    metrics: list[str]
    dimensions: list[str]
    filters: list[dict[str, str]]
    time_range: dict[str, object]
    candidate_tables: list[str]
    selected_tables: list[str]
    selected_columns: dict[str, list[str]]
    rejected_tables: list[RejectedTable]
    join_plan: JoinPlan
    historical_sql_examples: list[HistoricalSQLExample]
    generated_sql: str | None
    validation_report: ValidationReport
    validation_errors: list[str]
    retry_count: int
    repairable: bool
    sql_changed: bool
    execution_result: ExecutionResult
    explanation: str
    confidence: float
    trace_steps: Annotated[list[TraceStep], add]
