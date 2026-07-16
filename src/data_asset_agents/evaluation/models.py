from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from data_asset_agents.text2sql.models import (
    ExecutionResult,
    JoinPlan,
    SemanticQuery,
    TraceStep,
    ValidationReport,
)
from data_asset_agents.validation.evaluation_policy import EvaluationPolicyReport

QueryMode = Literal["schema", "rag", "ontology"]
StrategyVariant = Literal[
    "schema",
    "rag",
    "ontology_no_sql_asset",
    "ontology_full",
]


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class StrategyResult(BaseModel):
    """Uniform result contract across strictly isolated query strategies."""

    question: str
    query_mode: QueryMode
    strategy_variant: StrategyVariant
    status: Literal["success", "unsupported", "clarification_required", "failed"]
    error_code: str | None = None
    unsupported_reason: str | None = None
    generated_sql: str | None = None
    raw_model_output: str | None = None
    retrieved_context: list[dict[str, Any]] | None = None
    selected_tables: list[str] = Field(default_factory=list)
    selected_columns: dict[str, list[str]] = Field(default_factory=dict)
    discovered_joins: list[str] = Field(default_factory=list)
    semantic_query: SemanticQuery | None = None
    join_plan: JoinPlan | None = None
    sql_asset_candidates: list[dict[str, Any]] | None = None
    selected_sql_asset: dict[str, Any] | None = None
    selected_template_rank: int | None = None
    template_rejection_reasons: dict[str, list[str]] = Field(default_factory=dict)
    sql_rewrite: dict[str, Any] | None = None
    common_validation_report: ValidationReport | None = None
    ontology_policy_report: ValidationReport | None = None
    evaluation_policy_report: EvaluationPolicyReport | None = None
    execution_result: ExecutionResult | None = None
    latency_ms: float = 0.0
    token_usage: TokenUsage | None = None
    trace_steps: list[TraceStep] = Field(default_factory=list)
    mode_metadata: dict[str, Any] = Field(default_factory=dict)

