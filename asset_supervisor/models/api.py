"""总控FastAPI请求与统一结果模型。"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import ApiModel
from .execution_plan import ExecutionPlan
from .reflection import ResultReflection
from .task_requests import CommonContext


class PreferredScene(StrEnum):
    AUTO = "AUTO"
    ASSET_DUPLICATE = "ASSET_DUPLICATE"
    SQL_GENERATION = "SQL_GENERATION"
    LINEAGE_PARSING = "LINEAGE_PARSING"


class AgentScene(StrEnum):
    ASSET_DUPLICATE = "ASSET_DUPLICATE"
    SQL_GENERATION = "SQL_GENERATION"
    LINEAGE_PARSING = "LINEAGE_PARSING"
    CLARIFICATION = "CLARIFICATION"


class ResultStatus(StrEnum):
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    NEED_MORE_INFORMATION = "NEED_MORE_INFORMATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class FeedbackDecision(StrEnum):
    ACCEPT = "ACCEPT"
    EDIT_AND_ACCEPT = "EDIT_AND_ACCEPT"
    REJECT = "REJECT"


class ChatContext(ApiModel):
    department: str | None = None
    project_code: str | None = None
    metadata_version: str | None = None
    sql_dialect: str | None = None
    schema_scope: list[str] = Field(default_factory=list)
    script_id: str | None = None
    target_field: str | None = None

    def common_context(self) -> CommonContext:
        return CommonContext(
            department=self.department,
            project_code=self.project_code,
            metadata_version=self.metadata_version,
            permission_scopes=[],
        )


class ChatRequest(ApiModel):
    conversation_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4000)
    preferred_scene: PreferredScene = PreferredScene.AUTO
    context: ChatContext = Field(default_factory=ChatContext)
    correction_context: list[dict[str, Any]] = Field(default_factory=list)


class Provenance(ApiModel):
    service_version: str
    model_version: str
    prompt_version: str
    metadata_version: str


class ErrorDetail(ApiModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class TaskResultResponse(ApiModel):
    conversation_id: str
    task_id: str
    trace_id: str
    result_id: str
    scene: AgentScene
    status: ResultStatus
    version: int = Field(ge=1)
    summary: str
    result: dict[str, Any] | None
    required_information: list[str] = Field(default_factory=list)
    execution_plan: ExecutionPlan | None = None
    reflection: ResultReflection | None = None
    provenance: Provenance
    error: ErrorDetail | None = None
    created_at: str


class FeedbackRequest(ApiModel):
    conversation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    scene: AgentScene
    decision: FeedbackDecision
    rating: int | None = Field(default=None, ge=1, le=5)
    reason_codes: list[str] = Field(default_factory=list)
    comment: str | None = None
    edited_content: str | None = None
    retry: bool = False

    @model_validator(mode="after")
    def validate_feedback_rules(self) -> "FeedbackRequest":
        if self.retry and self.decision is not FeedbackDecision.REJECT:
            raise ValueError("只有 REJECT 可以触发重新生成")
        if self.decision is FeedbackDecision.REJECT:
            if not self.reason_codes or not self.comment:
                raise ValueError("REJECT 必须填写原因码和说明")
        if (
            self.decision is FeedbackDecision.EDIT_AND_ACCEPT
            and not self.edited_content
            and not self.comment
        ):
            raise ValueError("EDIT_AND_ACCEPT 必须提供修改内容或说明")
        return self


class FeedbackResponse(ApiModel):
    feedback_id: str
    accepted: bool
    retry_triggered: bool
    next_result_version: int | None = Field(default=None, ge=1)
    task_status: ResultStatus
    result: TaskResultResponse | None
    stored_at: str


class ConversationSummary(ApiModel):
    conversation_id: str
    user_id: str
    title: str
    preferred_scene: PreferredScene
    message_count: int = Field(ge=0)
    created_at: str
    updated_at: str


class ConversationMessageResponse(ApiModel):
    message_id: str
    conversation_id: str
    role: Literal["USER", "ASSISTANT", "SYSTEM"]
    content: str
    sequence_number: int = Field(ge=1)
    task_id: str | None = None
    result_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ConversationDeleteResponse(ApiModel):
    conversation_id: str
    deleted: bool
    deleted_at: str


class ConversationClearResponse(ApiModel):
    deleted_count: int = Field(ge=0)
    deleted_at: str
