"""任务一、任务二的Tool入参与专业服务请求模型。"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from .base import ApiModel


class TaskType(StrEnum):
    ASSET_DUPLICATE = "ASSET_DUPLICATE"
    SQL_GENERATION = "SQL_GENERATION"
    LINEAGE_PARSING = "LINEAGE_PARSING"


class CommonContext(ApiModel):
    department: str | None = None
    project_code: str | None = None
    metadata_version: str | None = None
    permission_scopes: list[str] = Field(default_factory=list)


class AssetField(ApiModel):
    field_name: str
    data_type: str | None = None
    description: str | None = None


class DraftAsset(ApiModel):
    asset_name: str | None = None
    asset_description: str | None = None
    asset_type: str | None = None
    metric_definition: str | None = None
    grain: list[str] = Field(default_factory=list)
    asset_sql: str | None = None
    fields: list[AssetField] = Field(default_factory=list)
    source_tables: list[str] = Field(default_factory=list)
    upstream_asset_ids: list[str] = Field(default_factory=list)


class SimilarityWeights(ApiModel):
    semantic: float = Field(default=0.4, ge=0, le=1)
    logic: float = Field(default=0.35, ge=0, le=1)
    lineage: float = Field(default=0.25, ge=0, le=1)


class SearchOptions(ApiModel):
    top_k: int = Field(default=10, ge=1, le=100)
    min_score: float = Field(default=0.75, ge=0, le=1)
    candidate_scope: list[str] = Field(default_factory=list)
    weights: SimilarityWeights = Field(default_factory=SimilarityWeights)


class AssetDuplicateToolInput(ApiModel):
    draft_asset: DraftAsset
    search_options: SearchOptions = Field(default_factory=SearchOptions)


class FilterCondition(ApiModel):
    field: str
    operator: str
    value: Any


class TimeRange(ApiModel):
    start: str | None = None
    end: str | None = None
    relative_expression: str | None = None


class SqlRequirement(ApiModel):
    metric: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    time_range: TimeRange | None = None
    grain: list[str] = Field(default_factory=list)
    target_fields: list[str] = Field(default_factory=list)


class SqlExecutionContext(ApiModel):
    sql_dialect: str | None = None
    database_scope: list[str] = Field(default_factory=list)
    schema_scope: list[str] = Field(default_factory=list)
    available_table_ids: list[str] = Field(default_factory=list)


class SqlConstraints(ApiModel):
    read_only: bool = True
    max_rows: int = Field(default=100_000, ge=1)
    forbidden_operations: list[str] = Field(
        default_factory=lambda: ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER"]
    )
    sensitive_field_policy: str = "MASK_OR_EXCLUDE"


class SqlGenerationToolInput(ApiModel):
    requirement: SqlRequirement
    execution_context: SqlExecutionContext = Field(default_factory=SqlExecutionContext)
    constraints: SqlConstraints = Field(default_factory=SqlConstraints)


class TaskRequestBase(ApiModel):
    request_id: str
    task_id: str
    trace_id: str
    conversation_id: str
    user_id: str
    scene: TaskType
    timestamp: str
    original_query: str
    context: CommonContext
    correction_context: list[dict[str, Any]] = Field(default_factory=list)


class AssetDuplicateCheckRequest(TaskRequestBase):
    scene: Literal[TaskType.ASSET_DUPLICATE] = TaskType.ASSET_DUPLICATE
    draft_asset: DraftAsset
    search_options: SearchOptions


class SqlGenerationRequest(TaskRequestBase):
    scene: Literal[TaskType.SQL_GENERATION] = TaskType.SQL_GENERATION
    requirement: SqlRequirement
    execution_context: SqlExecutionContext
    constraints: SqlConstraints


class LineageParseOptions(ApiModel):
    include_all_paths: bool = True
    max_nesting_depth: int = Field(default=10, ge=1)
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)


class LineageToolInput(ApiModel):
    script_id: str | None = None
    target_field: str | None = None
    sql_dialect: str | None = None
    options: LineageParseOptions = Field(default_factory=LineageParseOptions)


class LineageJobRequest(TaskRequestBase):
    scene: Literal[TaskType.LINEAGE_PARSING] = TaskType.LINEAGE_PARSING
    script_id: str
    target_field: str
    sql_dialect: str | None = None
    options: LineageParseOptions = Field(default_factory=LineageParseOptions)
