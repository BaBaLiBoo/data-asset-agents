"""Supervisor生成并约束执行过程的显式任务计划。"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from .base import ApiModel


class ExecutionPlanStatus(StrEnum):
    WAITING_INPUT = "WAITING_INPUT"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class ExecutionStepStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WAITING = "WAITING"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ExecutionStepCode(StrEnum):
    UNDERSTAND = "UNDERSTAND"
    ROUTE = "ROUTE"
    VALIDATE = "VALIDATE"
    EXECUTE = "EXECUTE"
    INSPECT = "INSPECT"
    REFLECT = "REFLECT"
    AWAIT_USER = "AWAIT_USER"
    AWAIT_REVIEW = "AWAIT_REVIEW"
    COMPLETE = "COMPLETE"


class ProfessionalToolName(StrEnum):
    CHECK_ASSET_DUPLICATE = "check_asset_duplicate"
    GENERATE_SQL = "generate_sql"
    PARSE_REGULATORY_LINEAGE = "parse_regulatory_lineage"


class ExecutionPlanStep(ApiModel):
    code: ExecutionStepCode
    label: str
    detail: str
    status: ExecutionStepStatus
    tool: ProfessionalToolName | None = None


class ExecutionPlanConstraints(ApiModel):
    max_professional_tool_calls: int = Field(default=1, ge=1, le=1)
    professional_tool_calls: int = Field(default=0, ge=0, le=1)
    allow_cross_scene: bool = False
    require_human_review: bool = True


class ExecutionPlan(ApiModel):
    plan_id: str
    scene: Literal[
        "ASSET_DUPLICATE",
        "SQL_GENERATION",
        "LINEAGE_PARSING",
        "CLARIFICATION",
    ]
    selected_tool: ProfessionalToolName | None = None
    status: ExecutionPlanStatus
    steps: list[ExecutionPlanStep]
    constraints: ExecutionPlanConstraints


def complete_execution_plan_after_review(
    plan: ExecutionPlan | None,
) -> ExecutionPlan | None:
    """人工采纳后将等待确认的计划推进到完成。"""

    if plan is None:
        return None
    completed = plan.model_copy(deep=True)
    completed.status = ExecutionPlanStatus.COMPLETED
    for step in completed.steps:
        if step.code is ExecutionStepCode.AWAIT_REVIEW:
            step.status = ExecutionStepStatus.SUCCESS
            step.detail = "用户已确认并采纳当前专业结果"
    if not any(step.code is ExecutionStepCode.COMPLETE for step in completed.steps):
        completed.steps.append(
            ExecutionPlanStep(
                code=ExecutionStepCode.COMPLETE,
                label="任务完成",
                detail="专业结果已通过人工确认，任务闭环完成",
                status=ExecutionStepStatus.SUCCESS,
            )
        )
    return completed
