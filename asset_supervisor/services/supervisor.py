"""受控的需求理解、参数检查、Tool 执行和结果状态决策工作流。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from ..adapters import DownstreamServiceError, LineageClient
from ..agents.requirement_agent import (
    RequirementUnderstandingAgent,
    RequirementUnderstandingError,
)
from ..agents.result_reflection_agent import (
    ResultReflectionAgent,
)
from ..models.api import (
    AgentScene,
    ChatRequest,
    ErrorDetail,
    PreferredScene,
    Provenance,
    ResultStatus,
    TaskResultResponse,
)
from ..models.memory import ConversationMemory
from ..models.routing import RunContext, ToolSelection
from ..models.reflection import ResultReflection, unavailable_reflection
from ..tools.registry import ToolRegistry
from ..tools.task_tools import AnalyzeLineageTool, ToolInputError
from .execution_planning import ExecutionPlanBuilder, FailureStage
from .requirement_validation import RequirementValidator


ASSET_WORDS = ("重复", "查重", "相似资产", "资产复用", "冗余资产", "是否已经存在")
SQL_WORDS = ("sql", "查询", "统计", "汇总", "取数", "报表", "字段", "生成")
LINEAGE_WORDS = ("血缘", "来源", "上游", "下游", "算子", "路径", "解析", "溯源")
CONTEXT_REFERENCE_WORDS = (
    "改成",
    "调整为",
    "换成",
    "沿用",
    "刚才",
    "上次",
    "之前",
    "继续",
    "保持不变",
    "增加维度",
    "再加",
)

ProgressReporter = Callable[[dict[str, Any]], Awaitable[None]]


class SupervisorService:
    """一次请求按固定阶段推进，并且最多执行一个专业 Tool。"""

    def __init__(
        self,
        requirement_agent: RequirementUnderstandingAgent | None,
        tool_registry: ToolRegistry,
        requirement_validator: RequirementValidator | None = None,
        execution_plan_builder: ExecutionPlanBuilder | None = None,
        reflection_agent: ResultReflectionAgent | None = None,
        lineage_client: LineageClient | None = None,
    ) -> None:
        self._requirement_agent = requirement_agent
        self._tool_registry = tool_registry
        self._requirement_validator = requirement_validator or RequirementValidator()
        self._execution_plan_builder = (
            execution_plan_builder or ExecutionPlanBuilder()
        )
        self._reflection_agent = reflection_agent
        self._lineage_client = lineage_client
        self._logger = logging.getLogger(self.__class__.__name__)

    async def handle(
        self,
        request: ChatRequest,
        *,
        user_id: str = "anonymous",
        task_id: str | None = None,
        result_version: int = 1,
        correction_context: list[dict[str, Any]] | None = None,
        conversation_memory: ConversationMemory | None = None,
        selection_override: ToolSelection | None = None,
        progress_reporter: ProgressReporter | None = None,
        report_understanding: bool = True,
    ) -> TaskResultResponse:
        run_context = RunContext.create(
            conversation_id=request.conversation_id,
            original_query=request.message,
            user_id=user_id,
            common_context=request.context.common_context(),
            task_id=task_id,
            result_version=result_version,
            correction_context=(
                correction_context
                if correction_context is not None
                else request.correction_context
            ),
        )
        selected_scene: AgentScene | None = None
        selected_tool_name: str | None = None
        professional_tool_calls = 0
        current_stage: FailureStage = "ROUTE"

        async def report_step(
            step_id: str,
            label: str,
            detail: str,
            status: str,
            *,
            tool: str | None = None,
        ) -> None:
            if progress_reporter is None:
                return
            payload: dict[str, Any] = {
                "id": step_id,
                "label": label,
                "detail": detail,
                "status": status,
            }
            if tool is not None:
                payload["tool"] = tool
            await progress_reporter(payload)

        def finalize(
            result: TaskResultResponse,
            *,
            failure_stage: FailureStage | None = None,
        ) -> TaskResultResponse:
            result.execution_plan = self._execution_plan_builder.build(
                run_context,
                result,
                selected_tool=selected_tool_name,
                professional_tool_calls=professional_tool_calls,
                failure_stage=failure_stage,
            )
            return result

        try:
            self._logger.info("workflow=UNDERSTAND trace_id=%s", run_context.trace_id)
            if report_understanding:
                await report_step(
                    "understand",
                    "理解并结构化需求",
                    "正在识别意图并提取业务参数",
                    "RUNNING",
                )
            selection = selection_override or await self._select_tool(
                request, conversation_memory=conversation_memory
            )
            if report_understanding:
                await report_step(
                    "understand",
                    "理解并结构化需求",
                    "已完成意图识别和业务参数提取",
                    "SUCCESS",
                )
            if not selection.tool_name:
                required_tool = self._required_tool(request.preferred_scene)
                if required_tool == "check_asset_duplicate":
                    supported_scenes = [AgentScene.ASSET_DUPLICATE]
                elif required_tool == "generate_sql":
                    supported_scenes = [AgentScene.SQL_GENERATION]
                elif required_tool == "parse_regulatory_lineage":
                    supported_scenes = [AgentScene.LINEAGE_PARSING]
                else:
                    supported_scenes = [
                        AgentScene.ASSET_DUPLICATE,
                        AgentScene.SQL_GENERATION,
                        AgentScene.LINEAGE_PARSING,
                    ]
                await report_step(
                    "route",
                    "确认任务场景",
                    selection.assistant_message or "需要用户补充后才能确认场景",
                    "WAITING",
                )
                return finalize(
                    self._clarification(
                        run_context,
                        question=selection.assistant_message
                        or "请说明需要检查重复资产，还是需要生成 SQL。",
                        required_information=(
                            ["message"]
                            if self._is_context_dependent_query(request.message)
                            else ["preferredScene"]
                        ),
                        supported_scenes=supported_scenes,
                    )
                )

            tool = self._tool_registry.get(selection.tool_name)
            selected_tool_name = tool.name
            selected_scene = AgentScene(tool.task_type.value)
            current_stage = "VALIDATE"

            await report_step(
                "route",
                f"确认{selected_scene.value}场景",
                f"已锁定专业能力 {tool.name}",
                "SUCCESS",
                tool=tool.name,
            )

            self._logger.info(
                "workflow=VALIDATE trace_id=%s tool=%s",
                run_context.trace_id,
                tool.name,
            )
            await report_step(
                "validate",
                "检查必要信息",
                "正在检查参数格式和最低业务信息",
                "RUNNING",
                tool=tool.name,
            )
            assessment = self._requirement_validator.assess(
                tool,
                selection.arguments,
                request,
            )
            if not assessment.is_complete:
                await report_step(
                    "validate",
                    "检查必要信息",
                    assessment.question,
                    "WAITING",
                    tool=tool.name,
                )
                return finalize(
                    self._clarification(
                        run_context,
                        question=assessment.question,
                        required_information=list(assessment.missing_fields),
                        supported_scenes=[selected_scene],
                    )
                )

            await report_step(
                "validate",
                "检查必要信息",
                "参数格式和最低业务信息检查通过",
                "SUCCESS",
                tool=tool.name,
            )

            prepared_plan = self._execution_plan_builder.prepare(
                run_context,
                selected_tool=tool.name,
            )
            if prepared_plan.selected_tool is None:
                raise ValueError("执行计划没有选择专业Tool")
            planned_tool = self._tool_registry.get(
                prepared_plan.selected_tool.value
            )
            if planned_tool.name != tool.name:
                raise ValueError("计划选择与需求路由的Tool不一致")

            current_stage = "EXECUTE"
            self._logger.info(
                "workflow=EXECUTE trace_id=%s tool=%s",
                run_context.trace_id,
                tool.name,
            )
            professional_tool_calls = 1
            await report_step(
                "execute",
                f"调用{selected_scene.value}智能体",
                f"正在执行 {tool.name}",
                "RUNNING",
                tool=tool.name,
            )
            if isinstance(planned_tool, AnalyzeLineageTool):
                lineage_result = await self._accept_lineage_job(
                        planned_tool,
                        assessment.arguments,
                        run_context,
                        selected_scene,
                    )
                await report_step(
                    "execute",
                    f"调用{selected_scene.value}智能体",
                    "专业服务已接受任务",
                    "SUCCESS",
                    tool=tool.name,
                )
                return finalize(lineage_result)
            downstream = await planned_tool.execute(
                assessment.arguments,
                run_context,
            )
            await report_step(
                "execute",
                f"调用{selected_scene.value}智能体",
                "专业服务执行完成",
                "SUCCESS",
                tool=tool.name,
            )

            current_stage = "INSPECT"
            self._logger.info(
                "workflow=REVIEW trace_id=%s status=%s",
                run_context.trace_id,
                downstream.get("status"),
            )
            await report_step(
                "inspect",
                "检查专业结果",
                "正在校验专业服务返回的结构和状态",
                "RUNNING",
                tool=tool.name,
            )
            inspected = self._inspect_downstream(
                run_context,
                expected_scene=selected_scene,
                downstream=downstream,
            )
            await report_step(
                "inspect",
                "检查专业结果",
                "专业结果结构和状态检查完成",
                "FAILED" if inspected.status is ResultStatus.FAILED else "SUCCESS",
                tool=tool.name,
            )
            failure_stage: FailureStage | None = None
            if inspected.status is ResultStatus.FAILED:
                failure_stage = (
                    "EXECUTE"
                    if downstream.get("status") == ResultStatus.FAILED.value
                    else "INSPECT"
                )
            elif inspected.status in (
                ResultStatus.SUCCESS,
                ResultStatus.REVIEW_REQUIRED,
            ):
                await report_step(
                    "reflect",
                    "反思需求覆盖情况",
                    "正在对专业结果进行只读语义检查",
                    "RUNNING",
                    tool=tool.name,
                )
                inspected.reflection = await self._reflect_result(
                    request=request,
                    scene=selected_scene,
                    structured_arguments=assessment.arguments,
                    professional_result=inspected.result or {},
                    conversation_memory=conversation_memory,
                )
                await report_step(
                    "reflect",
                    "反思需求覆盖情况",
                    "需求覆盖检查完成，等待人工确认",
                    "SUCCESS",
                    tool=tool.name,
                )
            return finalize(inspected, failure_stage=failure_stage)
        except DownstreamServiceError as exc:
            self._logger.warning("专业服务调用失败: %s", exc.message)
            await report_step(
                current_stage.lower(),
                "执行专业流程",
                exc.message,
                "FAILED",
                tool=selected_tool_name,
            )
            return finalize(
                self._failed(
                    run_context,
                    scene=selected_scene or AgentScene.CLARIFICATION,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                ),
                failure_stage="EXECUTE",
            )
        except ToolInputError as exc:
            self._logger.warning("结构化需求参数不合法: %s", exc)
            professional_tool_calls = 0
            await report_step(
                "validate",
                "检查必要信息",
                str(exc),
                "FAILED",
                tool=selected_tool_name,
            )
            return finalize(
                self._clarification(
                    run_context,
                    question="需求中的部分信息格式无法识别，请换一种方式补充业务口径和条件。",
                    required_information=["message"],
                    supported_scenes=[selected_scene] if selected_scene else [],
                ),
                failure_stage="VALIDATE",
            )
        except (KeyError, ValueError) as exc:
            self._logger.warning("需求工作流执行失败: %s", exc)
            await report_step(
                current_stage.lower(),
                "执行专业流程",
                str(exc),
                "FAILED",
                tool=selected_tool_name,
            )
            return finalize(
                self._failed(
                    run_context,
                    scene=selected_scene or AgentScene.CLARIFICATION,
                    code="INVALID_REQUEST",
                    message=f"需求工作流执行失败：{exc}",
                    retryable=False,
                ),
                failure_stage=current_stage,
            )

    async def _select_tool(
        self,
        request: ChatRequest,
        *,
        conversation_memory: ConversationMemory | None = None,
    ) -> ToolSelection:
        required_tool = self._required_tool(request.preferred_scene)
        if self._requirement_agent is not None:
            try:
                return await self._requirement_agent.select_tool(
                    request.message,
                    required_tool=required_tool,
                    conversation_memory=conversation_memory,
                )
            except RequirementUnderstandingError as exc:
                # 模型或兼容接口暂时不可用时，仍允许显式场景和明显关键词继续工作。
                self._logger.warning("模型需求理解失败，降级到确定性路由: %s", exc)

        if self._is_context_dependent_query(request.message):
            return ToolSelection(
                tool_name=None,
                arguments={},
                assistant_message=(
                    "当前指令依赖历史内容，但模型暂时无法安全解析。"
                    "请补充完整的指标、维度、时间范围或要沿用的表。"
                ),
            )

        if required_tool == "check_asset_duplicate":
            return ToolSelection(
                tool_name=required_tool,
                arguments=self._asset_arguments(request),
                assistant_message="",
            )
        if required_tool == "generate_sql":
            return ToolSelection(
                tool_name=required_tool,
                arguments=self._sql_arguments(request),
                assistant_message="",
            )
        if required_tool == "parse_regulatory_lineage":
            return ToolSelection(
                tool_name=required_tool,
                arguments=self._lineage_arguments(request),
                assistant_message="",
            )

        lowered = request.message.lower()
        if any(word in lowered for word in ASSET_WORDS):
            return ToolSelection(
                tool_name="check_asset_duplicate",
                arguments=self._asset_arguments(request),
                assistant_message="",
            )
        if any(word in lowered for word in SQL_WORDS):
            return ToolSelection(
                tool_name="generate_sql",
                arguments=self._sql_arguments(request),
                assistant_message="",
            )
        if any(word in lowered for word in LINEAGE_WORDS):
            return ToolSelection(
                tool_name="parse_regulatory_lineage",
                arguments=self._lineage_arguments(request),
                assistant_message="",
            )
        return ToolSelection(
            tool_name=None,
            arguments={},
            assistant_message="当前描述不足以确定专业场景。",
        )

    async def _reflect_result(
        self,
        *,
        request: ChatRequest,
        scene: AgentScene,
        structured_arguments: dict[str, Any],
        professional_result: dict[str, Any],
        conversation_memory: ConversationMemory | None,
    ) -> ResultReflection:
        if self._reflection_agent is None:
            return unavailable_reflection(
                "智能检查模型未配置，专业结果保持不变，请人工确认。"
            )
        self._logger.info("workflow=REFLECT scene=%s", scene.value)
        try:
            return await self._reflection_agent.reflect(
                original_query=request.message,
                scene=scene,
                structured_arguments=structured_arguments,
                professional_result=professional_result,
                conversation_memory=conversation_memory,
            )
        except Exception as exc:
            self._logger.warning("结果反思不可用: %s", exc)
            return unavailable_reflection(
                "智能检查暂时不可用，专业结果保持不变，请人工确认。"
            )

    @staticmethod
    def _is_context_dependent_query(user_query: str) -> bool:
        lowered = user_query.lower()
        return any(word in lowered for word in CONTEXT_REFERENCE_WORDS)

    @staticmethod
    def _required_tool(preferred_scene: PreferredScene) -> str | None:
        if preferred_scene is PreferredScene.ASSET_DUPLICATE:
            return "check_asset_duplicate"
        if preferred_scene is PreferredScene.SQL_GENERATION:
            return "generate_sql"
        if preferred_scene is PreferredScene.LINEAGE_PARSING:
            return "parse_regulatory_lineage"
        return None

    @staticmethod
    def _asset_arguments(request: ChatRequest) -> dict[str, Any]:
        return {
            "draftAsset": {
                "assetDescription": request.message,
                "grain": [],
                "fields": [],
                "sourceTables": [],
                "upstreamAssetIds": [],
            },
            "searchOptions": {
                "topK": 10,
                "minScore": 0.75,
                "candidateScope": [],
                "weights": {
                    "semantic": 0.4,
                    "logic": 0.35,
                    "lineage": 0.25,
                },
            },
        }

    @staticmethod
    def _sql_arguments(request: ChatRequest) -> dict[str, Any]:
        return {
            "requirement": {
                "metric": [request.message],
                "dimensions": [],
                "filters": [],
                "grain": [],
                "targetFields": [],
            },
            "executionContext": {
                "sqlDialect": request.context.sql_dialect,
                "databaseScope": [],
                "schemaScope": request.context.schema_scope,
                "availableTableIds": [],
            },
            "constraints": {
                "readOnly": True,
                "maxRows": 100000,
                "forbiddenOperations": [
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "DROP",
                    "ALTER",
                ],
                "sensitiveFieldPolicy": "MASK_OR_EXCLUDE",
            },
        }

    @staticmethod
    def _lineage_arguments(request: ChatRequest) -> dict[str, Any]:
        return {
            "scriptId": request.context.script_id,
            "targetField": request.context.target_field,
            "sqlDialect": request.context.sql_dialect,
            "options": {
                "includeAllPaths": True,
                "maxNestingDepth": 10,
                "confidenceThreshold": 0.5,
            },
        }

    def _inspect_downstream(
        self,
        context: RunContext,
        *,
        expected_scene: AgentScene,
        downstream: dict[str, Any],
    ) -> TaskResultResponse:
        normalized = self._normalize(context, downstream)
        if normalized.scene is not expected_scene:
            return self._failed(
                context,
                scene=expected_scene,
                code="DOWNSTREAM_SCENE_MISMATCH",
                message="专业服务返回了与当前任务不一致的场景。",
                retryable=False,
            )
        if normalized.status is ResultStatus.PROCESSING:
            return self._failed(
                context,
                scene=expected_scene,
                code="DOWNSTREAM_INVALID_STATE",
                message="同步专业服务不应返回 PROCESSING 状态。",
                retryable=True,
            )
        if normalized.status is ResultStatus.NEED_MORE_INFORMATION:
            if (
                not normalized.result
                or normalized.result.get("kind") != "clarification"
            ):
                return self._failed(
                    context,
                    scene=expected_scene,
                    code="DOWNSTREAM_INVALID_STATE",
                    message="专业服务请求补充信息时未返回追问内容。",
                    retryable=False,
                )
            return normalized
        if normalized.status in (ResultStatus.SUCCESS, ResultStatus.REVIEW_REQUIRED):
            if normalized.result is None:
                return self._failed(
                    context,
                    scene=expected_scene,
                    code="DOWNSTREAM_INVALID_STATE",
                    message="专业服务成功返回时缺少结果内容。",
                    retryable=False,
                )
            return normalized
        if normalized.status is ResultStatus.FAILED and normalized.error is None:
            return self._failed(
                context,
                scene=expected_scene,
                code="DOWNSTREAM_FAILED",
                message=normalized.summary or "专业服务执行失败。",
                retryable=False,
            )
        return normalized

    @staticmethod
    def _normalize(
        context: RunContext,
        downstream: dict[str, Any],
    ) -> TaskResultResponse:
        return TaskResultResponse(
            conversation_id=context.conversation_id,
            task_id=downstream["taskId"],
            trace_id=downstream["traceId"],
            result_id=downstream["resultId"],
            scene=AgentScene(downstream["scene"]),
            status=ResultStatus(downstream["status"]),
            version=downstream["version"],
            summary=downstream["summary"],
            result=downstream.get("result"),
            required_information=downstream.get("requiredInformation", []),
            provenance=Provenance.model_validate(downstream["provenance"]),
            error=(
                ErrorDetail.model_validate(downstream["error"])
                if downstream.get("error")
                else None
            ),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    @staticmethod
    def _clarification(
        context: RunContext,
        *,
        question: str,
        required_information: list[str],
        supported_scenes: list[AgentScene],
    ) -> TaskResultResponse:
        return TaskResultResponse(
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            result_id=f"result_{context.task_id}_v{context.result_version}",
            scene=AgentScene.CLARIFICATION,
            status=ResultStatus.NEED_MORE_INFORMATION,
            version=context.result_version,
            summary=question,
            result={
                "kind": "clarification",
                "question": question,
                "supportedScenes": [scene.value for scene in supported_scenes],
            },
            required_information=required_information,
            provenance=SupervisorService._supervisor_provenance(context),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    @staticmethod
    def _failed(
        context: RunContext,
        *,
        scene: AgentScene,
        code: str,
        message: str,
        retryable: bool,
    ) -> TaskResultResponse:
        return TaskResultResponse(
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            result_id=f"result_{context.task_id}_v{context.result_version}",
            scene=scene,
            status=ResultStatus.FAILED,
            version=context.result_version,
            summary=message,
            result=None,
            required_information=[],
            provenance=SupervisorService._supervisor_provenance(context),
            error=ErrorDetail(
                code=code,
                message=message,
                retryable=retryable,
                details={},
            ),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    @staticmethod
    def _supervisor_provenance(context: RunContext) -> Provenance:
        return Provenance(
            service_version="supervisor-0.7.0",
            model_version="RULE_OR_CONFIGURED_LLM",
            prompt_version="supervisor-workflow-v4-result-reflection",
            metadata_version=context.common_context.metadata_version or "UNKNOWN",
        )

    @staticmethod
    def _lineage_provenance(context: RunContext) -> Provenance:
        return Provenance(
            service_version="lineage-supervisor-0.1.0",
            model_version="LINEAGE_MODEL",
            prompt_version="lineage-multi-dim-logic-chain-v1",
            metadata_version=context.common_context.metadata_version or "UNKNOWN",
        )

    async def _accept_lineage_job(
        self,
        tool: AnalyzeLineageTool,
        arguments: dict[str, Any],
        run_context: RunContext,
        selected_scene: AgentScene,
    ) -> TaskResultResponse:
        if self._lineage_client is None:
            return self._failed(
                run_context,
                scene=selected_scene,
                code="DOWNSTREAM_UNAVAILABLE",
                message="血缘专业服务未配置",
                retryable=True,
            )
        tool_input = tool.input_model.model_validate(arguments)
        job_request = tool.build_request(tool_input, run_context)
        job = await self._lineage_client.create_job(job_request)
        return self._lineage_accepted(run_context, selected_scene, job)

    @staticmethod
    def _lineage_accepted(
        context: RunContext,
        scene: AgentScene,
        job: dict[str, Any],
    ) -> TaskResultResponse:
        return TaskResultResponse(
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            result_id=f"result_{context.task_id}_v{context.result_version}",
            scene=scene,
            status=ResultStatus.PROCESSING,
            version=context.result_version,
            summary="血缘解析任务已受理，正在异步解析。",
            result={
                "kind": "lineage",
                "jobId": job["jobId"],
                "scriptId": job["scriptId"],
                "targetField": job["targetField"],
                "sqlDialect": None,
                "metrics": None,
                "paths": [],
                "graph": None,
                "graphSummary": None,
                "summary": "血缘解析任务已受理，请通过流式接口获取进度与结果。",
                "confidence": None,
                "warnings": [],
            },
            required_information=[],
            provenance=SupervisorService._lineage_provenance(context),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def _lineage_result(
        self,
        context: RunContext,
        scene: AgentScene,
        job: dict[str, Any],
    ) -> TaskResultResponse:
        result = job.get("result") or {}
        final = TaskResultResponse(
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            result_id=f"result_{context.task_id}_v{context.result_version}",
            scene=scene,
            status=ResultStatus.REVIEW_REQUIRED,
            version=context.result_version,
            summary=result.get("summary") or "血缘解析完成，等待人工确认。",
            result=result,
            required_information=[],
            provenance=SupervisorService._lineage_provenance(context),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        final.execution_plan = self._execution_plan_builder.build(
            context,
            final,
            selected_tool="parse_regulatory_lineage",
            professional_tool_calls=1,
        )
        return final

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        user_id: str = "anonymous",
        conversation_memory: ConversationMemory | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """以 SSE 事件流执行一次请求；血缘场景走异步任务进度推送。"""

        run_context = RunContext.create(
            conversation_id=request.conversation_id,
            original_query=request.message,
            user_id=user_id,
            common_context=request.context.common_context(),
        )

        pending_scene = (
            request.preferred_scene.value
            if request.preferred_scene is not PreferredScene.AUTO
            else AgentScene.CLARIFICATION.value
        )
        yield (
            "task",
            {
                "taskId": run_context.task_id,
                "resultId": f"result_{run_context.task_id}_v1",
                "scene": pending_scene,
                "status": ResultStatus.PROCESSING.value,
                "version": 1,
            },
        )
        yield (
            "step",
            {
                "id": "understand",
                "label": "理解并结构化需求",
                "detail": "正在识别意图并提取业务参数",
                "status": "RUNNING",
            },
        )

        selection = await self._select_tool(
            request,
            conversation_memory=conversation_memory,
        )
        yield (
            "step",
            {
                "id": "understand",
                "label": "理解并结构化需求",
                "detail": "已完成意图识别和业务参数提取",
                "status": "SUCCESS",
            },
        )
        if selection.tool_name != "parse_regulatory_lineage":
            progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            worker = asyncio.create_task(
                self.handle(
                    request,
                    user_id=user_id,
                    task_id=run_context.task_id,
                    conversation_memory=conversation_memory,
                    selection_override=selection,
                    progress_reporter=progress_queue.put,
                    report_understanding=False,
                )
            )
            while not worker.done() or not progress_queue.empty():
                try:
                    progress = await asyncio.wait_for(
                        progress_queue.get(), timeout=0.1
                    )
                except TimeoutError:
                    continue
                yield ("step", progress)
            final = await worker
            yield ("result", final.model_dump(mode="json", by_alias=True))
            yield ("done", {"status": final.status.value})
            return

        tool = self._tool_registry.get(selection.tool_name)
        selected_scene = AgentScene.LINEAGE_PARSING
        yield (
            "task",
            {
                "taskId": run_context.task_id,
                "resultId": f"result_{run_context.task_id}_v1",
                "scene": selected_scene.value,
                "status": ResultStatus.PROCESSING.value,
                "version": 1,
            },
        )

        assessment = self._requirement_validator.assess(
            tool,
            selection.arguments,
            request,
        )
        if not assessment.is_complete:
            clarification = self._clarification(
                run_context,
                question=assessment.question,
                required_information=list(assessment.missing_fields),
                supported_scenes=[selected_scene],
            )
            yield ("step", {
                "id": "await-user",
                "label": "等待用户补充",
                "detail": assessment.question,
                "status": "WAITING",
            })
            yield ("result", clarification.model_dump(mode="json", by_alias=True))
            yield ("done", {"status": clarification.status.value})
            return

        if self._lineage_client is None:
            yield ("error", {
                "code": "DOWNSTREAM_UNAVAILABLE",
                "message": "血缘专业服务未配置",
                "retryable": True,
            })
            yield ("done", {"status": "FAILED"})
            return

        try:
            tool_input = tool.input_model.model_validate(assessment.arguments)
            job_request = tool.build_request(tool_input, run_context)
            job = await self._lineage_client.create_job(job_request)
        except DownstreamServiceError as exc:
            yield ("error", {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            })
            yield ("done", {"status": "FAILED"})
            return

        job_id = job["jobId"]

        emitted_stages: set[str] = set()
        max_polls = 240
        for _ in range(max_polls):
            await asyncio.sleep(0.5)
            try:
                current = await self._lineage_client.get_job(job_id)
            except DownstreamServiceError as exc:
                yield ("error", {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                })
                yield ("done", {"status": "FAILED"})
                return

            for stage in current.get("stages", []):
                code = stage.get("code", "")
                status = stage.get("status", "")
                if code not in emitted_stages and status in (
                    "RUNNING",
                    "SUCCESS",
                    "FAILED",
                ):
                    emitted_stages.add(code)
                    yield ("step", {
                        "id": f"lineage-{code.lower()}",
                        "label": stage.get("label", code),
                        "detail": self._lineage_stage_detail(code, status),
                        "status": status,
                        "tool": "parse_regulatory_lineage",
                    })

            if current.get("status") == "COMPLETED":
                final = self._lineage_result(run_context, selected_scene, current)
                yield ("result", final.model_dump(mode="json", by_alias=True))
                yield ("done", {"status": final.status.value})
                return
            if current.get("status") == "FAILED":
                error = current.get("error") or {}
                yield ("error", {
                    "code": error.get("code", "MODEL_SERVICE_ERROR"),
                    "message": error.get("message", "血缘解析失败"),
                    "retryable": bool(error.get("retryable", False)),
                })
                yield ("done", {"status": "FAILED"})
                return

        yield ("error", {
            "code": "DOWNSTREAM_TIMEOUT",
            "message": "血缘解析任务超时",
            "retryable": True,
        })
        yield ("done", {"status": "FAILED"})

    @staticmethod
    def _lineage_stage_detail(code: str, status: str) -> str:
        details = {
            "PREPROCESSING": "切分脚本并提取目标字段上下文",
            "PARSING": "解析SQL语法并识别CTE与子查询",
            "EXTRACTING_OPERATORS": "提取WHERE/JOIN/CASE/AGG等算子",
            "INFERRING_PATHS": "穿透嵌套与关联推理来源路径",
            "CHECKING_CONSISTENCY": "校验图结构与字段来源一致性",
            "BUILDING_GRAPH": "生成算子级血缘DAG",
        }
        if status == "FAILED":
            return "阶段执行失败"
        return details.get(code, "处理中")
