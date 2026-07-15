from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class TimeRange(BaseModel):
    kind: Literal["relative_days", "absolute", "none"] = "none"
    days: int | None = None
    start: date | None = None
    end: date | None = None
    original_text: str | None = None


class QueryFilter(BaseModel):
    field: str
    operator: str = "="
    value: str
    source: Literal["user", "metric_policy", "system"] = "user"


class SemanticQuery(BaseModel):
    metric_names: list[str] = Field(default_factory=list)
    dimension_names: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    time_range: TimeRange = Field(default_factory=TimeRange)
    intent: Literal["aggregate", "detail", "unknown"] = "unknown"


class MatchedConcept(BaseModel):
    id: str
    name: str
    kind: str
    matched_text: str
    score: float = Field(ge=0, le=1)


class RejectedTable(BaseModel):
    name: str
    reason: str
    status: str
    replacement: str | None = None


class JoinStep(BaseModel):
    left_table: str
    right_table: str
    condition: str
    relationship: str


class JoinPlan(BaseModel):
    tables: list[str] = Field(default_factory=list)
    steps: list[JoinStep] = Field(default_factory=list)


class HistoricalSQLExample(BaseModel):
    question: str
    sql: str
    certified: bool = True
    similarity: float = 0.0


class ValidationReport(BaseModel):
    valid: bool
    read_only: bool = False
    dialect: str = "postgres"
    statement_type: str | None = None
    tables: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    formatted_sql: str | None = None
    explain_passed: bool | None = None


class ExecutionResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    explain_plan: list[str] = Field(default_factory=list)


class TraceStep(BaseModel):
    node: str
    status: Literal["completed", "failed", "skipped"] = "completed"
    summary: str


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    query_mode: Literal["ontology", "rag", "schema"] = "ontology"


class SemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


class SemanticResolveRequest(BaseModel):
    semantic_query: SemanticQuery


class QueryResponse(BaseModel):
    question: str
    query_mode: str
    semantic_query: SemanticQuery | None = None
    matched_concepts: list[MatchedConcept] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    candidate_tables: list[str] = Field(default_factory=list)
    selected_tables: list[str] = Field(default_factory=list)
    selected_columns: dict[str, list[str]] = Field(default_factory=dict)
    rejected_tables: list[RejectedTable] = Field(default_factory=list)
    join_plan: JoinPlan | None = None
    historical_sql_examples: list[HistoricalSQLExample] = Field(default_factory=list)
    generated_sql: str | None = None
    validation_report: ValidationReport | None = None
    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    execution_result: ExecutionResult | None = None
    explanation: str | None = None
    confidence: float = 0.0
    trace_steps: list[TraceStep] = Field(default_factory=list)

