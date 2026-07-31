import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class TimeRange(BaseModel):
    kind: Literal["relative_days", "absolute", "none"] = "none"
    days: int | None = None
    start: date | None = None
    end: date | None = None
    original_text: str | None = None


class QueryFilter(BaseModel):
    table: str | None = None
    field: str
    operator: str = "="
    value: str
    source: Literal["user", "metric_policy", "system"] = "user"


class SemanticFilter(BaseModel):
    concept_id: str
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "IN"] = "="
    value: str | list[str]


class SemanticOrderBy(BaseModel):
    target: str
    direction: Literal["asc", "desc"] = "desc"


class SemanticQuery(BaseModel):
    metric_ids: list[str] = Field(default_factory=list)
    dimension_ids: list[str] = Field(default_factory=list)
    metric_names: list[str] = Field(default_factory=list)
    dimension_names: list[str] = Field(default_factory=list)
    filters: list[SemanticFilter] = Field(default_factory=list)
    time_range: TimeRange = Field(default_factory=TimeRange)
    order_by: list[SemanticOrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1000)
    top_n: int | None = Field(default=None, ge=1, le=1000)
    intent: Literal["aggregate", "detail", "unknown"] = "unknown"
    confidence: float = Field(default=0.0, ge=0, le=1)
    clarification_required: bool = False
    clarification_question: str | None = None


class SemanticQueryDraft(BaseModel):
    """Provider-neutral Structured Output; physical identifiers are forbidden."""

    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[SemanticFilter] = Field(default_factory=list)
    time_range: TimeRange = Field(default_factory=TimeRange)
    order_by: list[SemanticOrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1000)
    top_n: int | None = Field(default=None, ge=1, le=1000)
    intent: Literal["aggregate", "detail", "unknown"] = "unknown"
    confidence: float = Field(default=0.0, ge=0, le=1)


class MatchedConcept(BaseModel):
    id: str
    name: str
    kind: str
    matched_text: str
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    exact_score: float = Field(default=0, ge=0, le=1)
    synonym_score: float = Field(default=0, ge=0, le=1)
    keyword_score: float = Field(default=0, ge=0, le=1)
    vector_score: float = Field(default=0, ge=0, le=1)

    @field_validator(
        "score",
        "exact_score",
        "synonym_score",
        "keyword_score",
        "vector_score",
        mode="before",
    )
    @classmethod
    def normalize_score(cls, value: Any) -> float:
        try:
            score = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(score):
            return 0.0
        return max(0.0, min(1.0, score))


class RejectedTable(BaseModel):
    name: str
    reason: str
    status: str
    replacement: str | None = None


class JoinStep(BaseModel):
    left_table: str
    right_table: str
    left_column: str
    right_column: str
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


class ValidationIssue(BaseModel):
    code: str
    message: str
    table: str | None = None
    column: str | None = None
    expected_value: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    read_only: bool = False
    dialect: str = "postgres"
    statement_type: str | None = None
    tables: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
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
    sql_asset_enabled: bool = True


class SemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


class SemanticResolveRequest(BaseModel):
    semantic_query: SemanticQuery


class QueryResponse(BaseModel):
    question: str
    query_mode: str
    strategy_variant: str | None = None
    status: Literal["success", "unsupported", "clarification_required", "failed"] = (
        "success"
    )
    error_code: str | None = None
    unsupported_reason: str | None = None
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
    sql_asset_candidates: list[dict[str, Any]] | None = None
    selected_sql_asset: dict[str, Any] | None = None
    selected_template_rank: int | None = None
    template_rejection_reasons: dict[str, list[str]] = Field(default_factory=dict)
    sql_rewrite: dict[str, Any] | None = None
    generated_sql: str | None = None
    raw_model_output: str | None = None
    retrieved_context: list[dict[str, Any]] | None = None
    validation_report: ValidationReport | None = None
    common_validation_report: ValidationReport | None = None
    ontology_policy_report: ValidationReport | None = None
    evaluation_policy_report: dict[str, Any] | None = None
    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    repairable: bool = False
    sql_changed: bool = False
    execution_result: ExecutionResult | None = None
    explanation: str | None = None
    confidence: float = 0.0
    trace_steps: list[TraceStep] = Field(default_factory=list)
    latency_ms: float = 0.0
    token_usage: dict[str, int] | None = None
    mode_metadata: dict[str, Any] = Field(default_factory=dict)
