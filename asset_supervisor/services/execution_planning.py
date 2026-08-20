"""从受控Supervisor状态生成白名单执行计划。"""

from __future__ import annotations

from typing import Literal

from ..models.api import AgentScene, ResultStatus, TaskResultResponse
from ..models.execution_plan import (
    ExecutionPlan,
    ExecutionPlanConstraints,
    ExecutionPlanStatus,
    ExecutionPlanStep,
    ExecutionStepCode,
    ExecutionStepStatus,
    ProfessionalToolName,
    complete_execution_plan_after_review,
)
from ..models.routing import RunContext
from ..models.reflection import ReflectionStatus


FailureStage = Literal["ROUTE", "VALIDATE", "EXECUTE", "INSPECT"]

TOOL_SCENES = {
    ProfessionalToolName.CHECK_ASSET_DUPLICATE: AgentScene.ASSET_DUPLICATE,
    ProfessionalToolName.GENERATE_SQL: AgentScene.SQL_GENERATION,
    ProfessionalToolName.PARSE_REGULATORY_LINEAGE: AgentScene.LINEAGE_PARSING,
}

SCENE_NAMES = {
    AgentScene.ASSET_DUPLICATE: "重复资产识别",
    AgentScene.SQL_GENERATION: "SQL生成",
    AgentScene.LINEAGE_PARSING: "血缘解析",
    AgentScene.CLARIFICATION: "需求澄清",
}


class ExecutionPlanBuilder:
    """只使用固定步骤构建计划，不接受模型自由扩展步骤或Tool。"""

    def prepare(
        self,
        context: RunContext,
        *,
        selected_tool: str,
    ) -> ExecutionPlan:
        """在专业调用前生成READY计划，后续执行必须使用计划中的唯一Tool。"""

        tool = self._tool(selected_tool)
        if tool is None:
            raise ValueError("READY计划必须选择一个专业Tool")
        target_scene = TOOL_SCENES[tool]
        scene_name = SCENE_NAMES[target_scene]
        return ExecutionPlan(
            plan_id=f"plan_{context.task_id}_v{context.result_version}",
            scene=target_scene.value,
            selected_tool=tool,
            status=ExecutionPlanStatus.READY,
            steps=[
                self._step(
                    ExecutionStepCode.UNDERSTAND,
                    "理解并结构化需求",
                    "结合当前消息和同会话有限记忆识别意图与业务参数",
                    ExecutionStepStatus.SUCCESS,
                ),
                self._step(
                    ExecutionStepCode.ROUTE,
                    f"确认{scene_name}场景",
                    "计划已锁定唯一专业能力，不允许跨场景执行",
                    ExecutionStepStatus.SUCCESS,
                ),
                self._step(
                    ExecutionStepCode.VALIDATE,
                    "检查必要信息",
                    "参数格式和最低业务信息检查通过",
                    ExecutionStepStatus.SUCCESS,
                ),
                self._step(
                    ExecutionStepCode.EXECUTE,
                    f"调用{scene_name}智能体",
                    "等待执行计划中的唯一专业Tool",
                    ExecutionStepStatus.WAITING,
                    tool=tool,
                ),
                self._step(
                    ExecutionStepCode.INSPECT,
                    "检查专业结果",
                    "等待专业结果返回",
                    ExecutionStepStatus.WAITING,
                ),
                self._step(
                    ExecutionStepCode.REFLECT,
                    "反思需求覆盖情况",
                    "等待对专业结果进行只读语义检查",
                    ExecutionStepStatus.WAITING,
                ),
                self._step(
                    ExecutionStepCode.AWAIT_REVIEW,
                    "等待人工确认",
                    "专业结果生成后需要人工确认",
                    ExecutionStepStatus.WAITING,
                ),
            ],
            constraints=ExecutionPlanConstraints(
                max_professional_tool_calls=1,
                professional_tool_calls=0,
                allow_cross_scene=False,
                require_human_review=True,
            ),
        )

    def build(
        self,
        context: RunContext,
        result: TaskResultResponse,
        *,
        selected_tool: str | None,
        professional_tool_calls: int,
        failure_stage: FailureStage | None = None,
    ) -> ExecutionPlan:
        tool = self._tool(selected_tool)
        if professional_tool_calls not in (0, 1):
            raise ValueError("一次请求最多调用一个专业Tool")
        if professional_tool_calls and tool is None:
            raise ValueError("存在专业Tool调用时必须记录selectedTool")

        target_scene = TOOL_SCENES.get(tool, result.scene)
        if tool is not None and target_scene not in (
            AgentScene.ASSET_DUPLICATE,
            AgentScene.SQL_GENERATION,
            AgentScene.LINEAGE_PARSING,
        ):
            raise ValueError("执行计划不能选择非专业场景")
        steps = [
            self._step(
                ExecutionStepCode.UNDERSTAND,
                "理解并结构化需求",
                "结合当前消息和同会话有限记忆识别意图与业务参数",
                ExecutionStepStatus.SUCCESS,
            )
        ]

        if tool is None:
            steps.extend(
                self._without_tool_steps(
                    result,
                    failure_stage=failure_stage,
                )
            )
        else:
            steps.extend(
                self._selected_tool_steps(
                    result,
                    tool=tool,
                    professional_tool_calls=professional_tool_calls,
                    failure_stage=failure_stage,
                )
            )

        return ExecutionPlan(
            plan_id=f"plan_{context.task_id}_v{context.result_version}",
            scene=target_scene.value,
            selected_tool=tool,
            status=self._plan_status(result.status),
            steps=steps,
            constraints=ExecutionPlanConstraints(
                max_professional_tool_calls=1,
                professional_tool_calls=professional_tool_calls,
                allow_cross_scene=False,
                require_human_review=True,
            ),
        )

    def complete_after_review(self, plan: ExecutionPlan | None) -> ExecutionPlan | None:
        """人工采纳后把等待确认的计划推进到完成，保留原计划和Tool计数。"""

        return complete_execution_plan_after_review(plan)

    def _without_tool_steps(
        self,
        result: TaskResultResponse,
        *,
        failure_stage: FailureStage | None,
    ) -> list[ExecutionPlanStep]:
        if result.status is ResultStatus.FAILED:
            return [
                self._step(
                    ExecutionStepCode.ROUTE,
                    "确认任务场景",
                    result.error.message if result.error else result.summary,
                    ExecutionStepStatus.FAILED,
                )
            ]
        return [
            self._step(
                ExecutionStepCode.ROUTE,
                "确认任务场景",
                "当前描述不足以安全确定唯一专业能力",
                (
                    ExecutionStepStatus.FAILED
                    if failure_stage == "ROUTE"
                    else ExecutionStepStatus.WAITING
                ),
            ),
            self._step(
                ExecutionStepCode.AWAIT_USER,
                "等待用户补充",
                self._question(result),
                ExecutionStepStatus.WAITING,
            ),
        ]

    def _selected_tool_steps(
        self,
        result: TaskResultResponse,
        *,
        tool: ProfessionalToolName,
        professional_tool_calls: int,
        failure_stage: FailureStage | None,
    ) -> list[ExecutionPlanStep]:
        target_scene = TOOL_SCENES[tool]
        scene_name = SCENE_NAMES[target_scene]
        steps = [
            self._step(
                ExecutionStepCode.ROUTE,
                f"确认{scene_name}场景",
                "计划已锁定唯一专业能力，不允许跨场景执行",
                ExecutionStepStatus.SUCCESS,
            )
        ]

        if professional_tool_calls == 0:
            validation_failed = result.status is ResultStatus.FAILED
            steps.append(
                self._step(
                    ExecutionStepCode.VALIDATE,
                    "检查必要信息",
                    (
                        result.error.message
                        if validation_failed and result.error
                        else self._question(result)
                    ),
                    (
                        ExecutionStepStatus.FAILED
                        if validation_failed or failure_stage == "VALIDATE"
                        else ExecutionStepStatus.WAITING
                    ),
                )
            )
            if result.status is ResultStatus.NEED_MORE_INFORMATION:
                steps.append(
                    self._step(
                        ExecutionStepCode.AWAIT_USER,
                        "等待用户补充",
                        self._question(result),
                        ExecutionStepStatus.WAITING,
                    )
                )
            return steps

        steps.append(
            self._step(
                ExecutionStepCode.VALIDATE,
                "检查必要信息",
                "参数格式和最低业务信息检查通过",
                ExecutionStepStatus.SUCCESS,
            )
        )
        execute_failed = (
            result.status is ResultStatus.FAILED
            and failure_stage != "INSPECT"
        )
        steps.append(
            self._step(
                ExecutionStepCode.EXECUTE,
                f"调用{scene_name}智能体",
                (
                    result.error.message
                    if execute_failed and result.error
                    else "通过受控HTTP接口执行计划中的唯一专业Tool"
                ),
                (
                    ExecutionStepStatus.FAILED
                    if execute_failed
                    else ExecutionStepStatus.SUCCESS
                ),
                tool=tool,
            )
        )
        if execute_failed:
            return steps

        if result.status is ResultStatus.FAILED:
            steps.append(
                self._step(
                    ExecutionStepCode.INSPECT,
                    "检查专业结果",
                    result.error.message if result.error else result.summary,
                    ExecutionStepStatus.FAILED,
                )
            )
            return steps

        if result.status is ResultStatus.NEED_MORE_INFORMATION:
            steps.extend(
                [
                    self._step(
                        ExecutionStepCode.INSPECT,
                        "检查专业结果",
                        "专业智能体需要更多业务信息才能继续",
                        ExecutionStepStatus.WAITING,
                    ),
                    self._step(
                        ExecutionStepCode.AWAIT_USER,
                        "等待用户补充",
                        self._question(result),
                        ExecutionStepStatus.WAITING,
                    ),
                ]
            )
            return steps

        steps.append(
            self._step(
                ExecutionStepCode.INSPECT,
                "检查专业结果",
                "场景、状态、结果内容和来源信息检查通过",
                ExecutionStepStatus.SUCCESS,
            )
        )
        reflection = result.reflection
        if reflection is None:
            steps.append(
                self._step(
                    ExecutionStepCode.REFLECT,
                    "反思需求覆盖情况",
                    "当前结果没有可用的语义检查意见",
                    ExecutionStepStatus.SKIPPED,
                )
            )
        elif reflection.status is ReflectionStatus.UNAVAILABLE:
            steps.append(
                self._step(
                    ExecutionStepCode.REFLECT,
                    "反思需求覆盖情况",
                    reflection.summary,
                    ExecutionStepStatus.SKIPPED,
                )
            )
        else:
            steps.append(
                self._step(
                    ExecutionStepCode.REFLECT,
                    "反思需求覆盖情况",
                    reflection.summary,
                    ExecutionStepStatus.SUCCESS,
                )
            )
        if result.status is ResultStatus.REVIEW_REQUIRED:
            steps.append(
                self._step(
                    ExecutionStepCode.AWAIT_REVIEW,
                    "等待人工确认",
                    "专业结果已生成，上线或复用前需要用户确认",
                    ExecutionStepStatus.WAITING,
                )
            )
        elif result.status is ResultStatus.SUCCESS:
            steps.append(
                self._step(
                    ExecutionStepCode.COMPLETE,
                    "任务完成",
                    "结果已通过总控检查并返回",
                    ExecutionStepStatus.SUCCESS,
                )
            )
        return steps

    @staticmethod
    def _tool(selected_tool: str | None) -> ProfessionalToolName | None:
        if selected_tool is None:
            return None
        try:
            return ProfessionalToolName(selected_tool)
        except ValueError as exc:
            raise ValueError(f"执行计划包含未知Tool: {selected_tool}") from exc

    @staticmethod
    def _plan_status(status: ResultStatus) -> ExecutionPlanStatus:
        return {
            ResultStatus.PROCESSING: ExecutionPlanStatus.RUNNING,
            ResultStatus.SUCCESS: ExecutionPlanStatus.COMPLETED,
            ResultStatus.NEED_MORE_INFORMATION: ExecutionPlanStatus.WAITING_INPUT,
            ResultStatus.REVIEW_REQUIRED: ExecutionPlanStatus.REVIEW,
            ResultStatus.FAILED: ExecutionPlanStatus.FAILED,
        }[status]

    @staticmethod
    def _question(result: TaskResultResponse) -> str:
        if result.result and result.result.get("kind") == "clarification":
            return str(result.result.get("question") or result.summary)
        return result.summary

    @staticmethod
    def _step(
        code: ExecutionStepCode,
        label: str,
        detail: str,
        status: ExecutionStepStatus,
        *,
        tool: ProfessionalToolName | None = None,
    ) -> ExecutionPlanStep:
        return ExecutionPlanStep(
            code=code,
            label=label,
            detail=detail,
            status=status,
            tool=tool,
        )
