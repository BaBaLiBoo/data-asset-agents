from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

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


class BenchmarkCase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    question: str = Field(min_length=2, max_length=1000)
    category: str
    difficulty: Literal["easy", "medium", "hard"]
    expected_status: Literal["success", "clarification_required", "unsupported"]
    gold_metric_ids: list[str] = Field(default_factory=list)
    gold_dimension_ids: list[str] = Field(default_factory=list)
    gold_filters: list[dict[str, Any]] = Field(default_factory=list)
    gold_semantic_filters: list[dict[str, Any]] = Field(default_factory=list)
    gold_time_range: dict[str, Any] = Field(default_factory=lambda: {"kind": "none"})
    gold_tables: list[str] = Field(default_factory=list)
    gold_columns: list[str] = Field(default_factory=list)
    gold_joins: list[str] = Field(default_factory=list)
    gold_sql: str | None = None
    expected_result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_order_sensitive: bool = False
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def validate_gold_contract(self) -> BenchmarkCase:
        if self.expected_status == "success":
            if not self.gold_sql:
                raise ValueError("success cases require gold_sql")
            if not self.gold_tables:
                raise ValueError("success cases require gold_tables")
            if not self.expected_result_hash:
                raise ValueError("success cases require expected_result_hash")
        return self


class BenchmarkSuite(BaseModel):
    version: str
    description: str
    cases: list[BenchmarkCase]

    @model_validator(mode="after")
    def unique_case_ids(self) -> BenchmarkSuite:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Benchmark case IDs must be unique")
        return self


class EvaluationRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    run_kind: Literal["smoke", "live"]
    query_mode: QueryMode
    strategy_variant: StrategyVariant
    sql_asset_enabled: bool = False
    model_provider: str
    model_name: str
    embedding_model: str | None = None
    temperature: float = 0.0
    max_output_tokens: int = 2048
    timeout_seconds: int = 30
    git_commit_sha: str
    strategy_version: str = "v1"
    prompt_version: str = "v1"
    database_snapshot_hash: str
    benchmark_hash: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    physical_rag_build_id: str | None = None
    ontology_version_id: str | None = None
    bundle_hash: str | None = None
    sql_asset_build_id: str | None = None
    experiment_group: Literal[
        "T-A", "T-B", "T-C", "T-D", "T-E", "T-F", "T-G", "T-H"
    ] | None = None
    ontology_source: Literal[
        "NONE", "REVIEWED_AUTO_SCAFFOLD", "REVIEWED_O_C", "REVIEWED_O_D", "GOLD"
    ] = "NONE"
    construction_run_id: str | None = None
    benchmark_version: str
    benchmark_path: str = "data/benchmark/text2sql_v1.json"
    random_seed: int = 20260716
    max_cases: int | None = None
    concurrency: int = Field(default=1, ge=1, le=16)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"] = "PENDING"
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_mode_provenance(self) -> EvaluationRun:
        if self.strategy_variant == "schema":
            if any(
                (
                    self.embedding_model,
                    self.physical_rag_build_id,
                    self.ontology_version_id,
                    self.sql_asset_build_id,
                )
            ):
                raise ValueError("schema runs cannot carry RAG/ontology/SQLAsset provenance")
            if self.construction_run_id or self.ontology_source != "NONE":
                raise ValueError("schema runs cannot carry construction provenance")
        elif self.strategy_variant == "rag":
            if not self.physical_rag_build_id:
                raise ValueError("rag runs require physical_rag_build_id")
            if self.ontology_version_id or self.sql_asset_build_id:
                raise ValueError("rag runs cannot carry ontology/SQLAsset provenance")
            if self.construction_run_id or self.ontology_source != "NONE":
                raise ValueError("rag runs cannot carry construction provenance")
        elif self.strategy_variant == "ontology_no_sql_asset":
            if not self.ontology_version_id or self.sql_asset_build_id:
                raise ValueError("ontology ablation requires ontology only")
            if self.sql_asset_enabled:
                raise ValueError("ontology ablation must disable SQLAsset")
        elif self.strategy_variant == "ontology_full":
            if not self.ontology_version_id or not self.sql_asset_build_id:
                raise ValueError("ontology full requires ontology and SQLAsset builds")
            if not self.sql_asset_enabled:
                raise ValueError("ontology full must enable SQLAsset")
        if self.ontology_source in {
            "REVIEWED_AUTO_SCAFFOLD",
            "REVIEWED_O_C",
            "REVIEWED_O_D",
        } and not self.construction_run_id:
            raise ValueError("reviewed ontology runs require construction_run_id")
        if self.ontology_source == "GOLD" and self.construction_run_id:
            raise ValueError("Gold ontology runs cannot carry construction_run_id")
        return self

    def mark_running(self) -> EvaluationRun:
        return self.model_copy(update={"status": "RUNNING", "started_at": datetime.now(UTC)})


class EvaluationCaseResult(BaseModel):
    run_id: str
    case_id: str
    question: str | None = None
    category: str | None = None
    difficulty: str | None = None
    gold: dict[str, Any] | None = None
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    predicted_status: str
    semantic_output: dict[str, Any] | None = None
    retrieved_context: list[dict[str, Any]] | None = None
    retrieved_tables: list[str] = Field(default_factory=list)
    retrieved_columns: list[str] = Field(default_factory=list)
    raw_model_output: str | None = None
    generated_sql: str | None = None
    referenced_tables: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)
    discovered_joins: list[str] = Field(default_factory=list)
    common_validation_errors: list[str] = Field(default_factory=list)
    ontology_policy_errors: list[str] | None = None
    evaluation_policy_violations: list[str] = Field(default_factory=list)
    execution_result_hash: str | None = None
    latency_ms: float = 0.0
    token_usage: TokenUsage | None = None
    template_adopted: bool | None = None
    template_compatible: bool | None = None
    sql_asset_candidates: list[dict[str, Any]] | None = None
    selected_sql_asset_id: str | None = None
    selected_template_rank: int | None = None
    template_rejection_reasons: dict[str, list[str]] = Field(default_factory=dict)
    sql_rewrite: dict[str, Any] | None = None
    success: bool
    failure_category: str | None = None
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluationMetrics(BaseModel):
    run_id: str
    case_count: int
    status_accuracy: float
    semantic_query_accuracy: float | None = None
    table_recall_at_k: float | None = None
    table_exact_match: float
    column_recall_at_k: float | None = None
    column_exact_match: float
    join_exact_match: float | None = None
    deprecated_table_false_selection_rate: float | None = None
    business_policy_accuracy: float | None = None
    sql_parse_rate: float
    sql_execution_rate: float
    result_accuracy: float | None = None
    template_adoption_rate: float | None = None
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    failure_distribution: dict[str, int] = Field(default_factory=dict)


class EvaluationRunRequest(BaseModel):
    query_mode: QueryMode
    strategy_variant: StrategyVariant
    sql_asset_enabled: bool = False
    benchmark_path: str = "data/benchmark/text2sql_v1.json"
    max_cases: int | None = Field(default=None, ge=1, le=1000)
    run_kind: Literal["smoke", "live"] = "smoke"
    concurrency: int = Field(default=1, ge=1, le=16)
    model_provider: str | None = None
    model_name: str | None = None
    experiment_group: Literal[
        "T-A", "T-B", "T-C", "T-D", "T-E", "T-F", "T-G", "T-H"
    ] | None = None
    ontology_source: Literal[
        "NONE", "REVIEWED_AUTO_SCAFFOLD", "REVIEWED_O_C", "REVIEWED_O_D", "GOLD"
    ] = "NONE"
    construction_run_id: str | None = None


class EvaluationComparison(BaseModel):
    runs: list[EvaluationMetrics]
    warnings: list[str] = Field(default_factory=list)
