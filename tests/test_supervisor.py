"""Supervisor 受控工作流的离线单元测试。"""

import asyncio
from typing import Any

from langchain_core.messages import AIMessage

from asset_supervisor.agents.requirement_agent import RequirementUnderstandingAgent
from asset_supervisor.agents.task_agents import TaskAgentPort
from asset_supervisor.models.api import (
    ChatRequest,
    PreferredScene,
    ResultStatus,
)
from asset_supervisor.models.reflection import (
    ReflectionCategory,
    ReflectionItem,
    ReflectionItemStatus,
    ReflectionRiskLevel,
    ReflectionStatus,
    ResultReflection,
)
from asset_supervisor.services.execution_planning import ExecutionPlanBuilder
from asset_supervisor.services.supervisor import SupervisorService
from asset_supervisor.tools.registry import ToolRegistry
from asset_supervisor.tools.task_tools import AssetDuplicateCheckTool, GenerateSqlTool


class FakeToolCallingModel:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.tools: list[dict[str, Any]] = []
        self.bind_options: dict[str, Any] = {}

    def bind_tools(self, tools, **kwargs):
        self.tools = list(tools)
        self.bind_options = kwargs
        assert kwargs["parallel_tool_calls"] is False
        return self

    async def ainvoke(self, messages):
        assert len(messages) in (2, 3)
        return self.response


class RecordingTaskAgent(TaskAgentPort):
    def __init__(
        self,
        scene: str,
        *,
        status: str = "REVIEW_REQUIRED",
        include_error: bool = False,
    ) -> None:
        self.scene = scene
        self.status = status
        self.include_error = include_error
        self.requests = []

    async def execute(self, request) -> dict[str, Any]:
        self.requests.append(request)
        result = self._result()
        if self.status == "NEED_MORE_INFORMATION":
            result = {
                "kind": "clarification",
                "question": "请补充指标口径。",
                "supportedScenes": [self.scene],
            }
        elif self.status == "FAILED":
            result = None

        return {
            "requestId": request.request_id,
            "taskId": request.task_id,
            "traceId": request.trace_id,
            "resultId": f"result_{request.task_id}_v1",
            "scene": self.scene,
            "status": self.status,
            "version": 1,
            "summary": "专业服务测试结果",
            "confidence": 0.9,
            "requiredInformation": (
                ["requirement.metric"]
                if self.status == "NEED_MORE_INFORMATION"
                else []
            ),
            "result": result,
            "provenance": {
                "serviceVersion": "test-1.0",
                "modelVersion": "test-model",
                "promptVersion": "test-prompt",
                "metadataVersion": "test-meta",
            },
            "latencyMs": 1,
            "error": (
                {
                    "code": "TEST_FAILURE",
                    "message": "测试失败",
                    "retryable": False,
                    "details": {},
                }
                if self.include_error
                else None
            ),
        }

    def _result(self) -> dict[str, Any]:
        if self.scene == "ASSET_DUPLICATE":
            return {
                "kind": "duplicate",
                "canReuse": True,
                "recommendation": "REUSE_EXISTING_ASSET",
                "threshold": 0.85,
                "candidates": [],
            }
        return {
            "kind": "sql",
            "template": None,
            "sqlDialect": "HIVE_SQL",
            "sql": "SELECT 1",
            "recommendedTables": [],
            "recommendedFields": [],
            "explanation": "离线测试",
            "assumptions": [],
            "validation": {
                "passed": True,
                "riskLevel": "LOW",
                "checks": [],
                "warnings": [],
            },
        }


class CrossScenePlanBuilder(ExecutionPlanBuilder):
    def prepare(self, context, *, selected_tool):
        return super().prepare(
            context,
            selected_tool=(
                "check_asset_duplicate"
                if selected_tool == "generate_sql"
                else "generate_sql"
            ),
        )


class RecordingReflectionAgent:
    def __init__(self, response: ResultReflection) -> None:
        self.response = response
        self.calls = []

    async def reflect(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FailingReflectionAgent:
    async def reflect(self, **kwargs):
        raise RuntimeError("reflection model unavailable")


def build_service(
    response: AIMessage | None = None,
    *,
    task_one_status: str = "REVIEW_REQUIRED",
    task_two_status: str = "REVIEW_REQUIRED",
    execution_plan_builder: ExecutionPlanBuilder | None = None,
    reflection_agent=None,
):
    task_one = RecordingTaskAgent("ASSET_DUPLICATE", status=task_one_status)
    task_two = RecordingTaskAgent("SQL_GENERATION", status=task_two_status)
    registry = ToolRegistry(
        [AssetDuplicateCheckTool(task_one), GenerateSqlTool(task_two)]
    )
    model = FakeToolCallingModel(response) if response is not None else None
    agent = (
        RequirementUnderstandingAgent(model, registry.llm_definitions())
        if model is not None
        else None
    )
    return (
        SupervisorService(
            agent,
            registry,
            execution_plan_builder=execution_plan_builder,
            reflection_agent=reflection_agent,
        ),
        task_one,
        task_two,
        model,
    )


def test_chat_request_accepts_camel_case_correction_context() -> None:
    request = ChatRequest.model_validate(
        {
            "conversationId": "conv_correction_context",
            "message": "按检查意见重新生成SQL",
            "preferredScene": "SQL_GENERATION",
            "correctionContext": [
                {
                    "previousResultId": "result_previous",
                    "decision": "REJECT",
                    "reasonCode": "WRONG_FIELD",
                    "comment": "字段应改为first_open_date",
                }
            ],
        }
    )

    assert request.correction_context[0]["reasonCode"] == "WRONG_FIELD"


def test_sql_stream_emits_each_completed_stage_before_the_result() -> None:
    service, _, task_two, _ = build_service()

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        async for name, data in service.stream_chat(
            ChatRequest(
                conversation_id="conv_sql_stream",
                message="生成最近30天各分行交易金额SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        ):
            events.append((name, data))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    result_index = names.index("result")
    steps = [data for name, data in events[:result_index] if name == "step"]

    assert names[0] == "task"
    assert names[-1] == "done"
    assert [step["id"] for step in steps if step["status"] == "SUCCESS"] == [
        "understand",
        "route",
        "validate",
        "execute",
        "inspect",
        "reflect",
    ]
    assert any(
        step["id"] == "execute" and step["status"] == "RUNNING"
        for step in steps
    )
    assert len(task_two.requests) == 1


def test_reflection_warning_is_attached_without_a_second_professional_call() -> None:
    reflection_agent = RecordingReflectionAgent(
        ResultReflection(
            status=ReflectionStatus.WARNING,
            summary="时间范围需要人工确认",
            risk_level=ReflectionRiskLevel.MEDIUM,
            items=[
                ReflectionItem(
                    category=ReflectionCategory.TIME_RANGE,
                    status=ReflectionItemStatus.WARNING,
                    requirement="最近180天",
                    observation="专业结果使用最近90天",
                    suggestion="确认后通过反馈闭环重新生成",
                )
            ],
        )
    )
    service, task_one, task_two, _ = build_service(
        reflection_agent=reflection_agent
    )

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_reflection_warning",
                message="生成最近180天新增客户数SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.reflection.status is ReflectionStatus.WARNING
    assert len(reflection_agent.calls) == 1
    assert len(task_two.requests) == 1
    assert task_one.requests == []
    assert result.execution_plan.constraints.professional_tool_calls == 1
    reflect_step = next(
        step
        for step in result.execution_plan.steps
        if step.code.value == "REFLECT"
    )
    assert reflect_step.status.value == "SUCCESS"
    assert reflect_step.detail == "时间范围需要人工确认"


def test_missing_reflection_model_does_not_block_professional_result() -> None:
    service, _, task_two, _ = build_service()

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_reflection_unavailable",
                message="生成最近90天新增客户数SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status is ResultStatus.REVIEW_REQUIRED
    assert result.reflection.status is ReflectionStatus.UNAVAILABLE
    assert len(task_two.requests) == 1
    reflect_step = next(
        step
        for step in result.execution_plan.steps
        if step.code.value == "REFLECT"
    )
    assert reflect_step.status.value == "SKIPPED"


def test_reflection_failure_degrades_without_retrying_professional_tool() -> None:
    service, _, task_two, _ = build_service(
        reflection_agent=FailingReflectionAgent()
    )

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_reflection_failure",
                message="生成最近90天新增客户数SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status is ResultStatus.REVIEW_REQUIRED
    assert result.reflection.status is ReflectionStatus.UNAVAILABLE
    assert len(task_two.requests) == 1
    assert result.execution_plan.constraints.professional_tool_calls == 1


def test_explicit_asset_scene_routes_without_model_when_model_is_disabled() -> None:
    service, task_one, task_two, _ = build_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_asset",
                message="检查客户月交易汇总资产是否重复",
                preferred_scene=PreferredScene.ASSET_DUPLICATE,
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert result.scene.value == "ASSET_DUPLICATE"
    assert result.result["kind"] == "duplicate"
    assert len(task_one.requests) == 1
    assert task_two.requests == []
    assert result.execution_plan.status.value == "REVIEW"
    assert result.execution_plan.selected_tool.value == "check_asset_duplicate"
    assert result.execution_plan.constraints.professional_tool_calls == 1
    assert result.execution_plan.constraints.max_professional_tool_calls == 1
    assert result.execution_plan.constraints.allow_cross_scene is False
    assert result.execution_plan.steps[-1].code.value == "AWAIT_REVIEW"


def test_explicit_sql_scene_routes_without_model_when_model_is_disabled() -> None:
    service, task_one, task_two, _ = build_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_sql",
                message="生成各分行新增客户数 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
                context={"sqlDialect": "HIVE_SQL", "schemaScope": ["card_dm"]},
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert result.scene.value == "SQL_GENERATION"
    assert result.result["sql"] == "SELECT 1"
    assert task_one.requests == []
    assert len(task_two.requests) == 1


def test_explicit_scene_uses_model_to_extract_structured_parameters() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "generate_sql",
                "args": {
                    "requirement": {
                        "metric": ["新增客户数"],
                        "dimensions": ["分行"],
                    }
                },
                "id": "call_explicit",
                "type": "tool_call",
            }
        ],
    )
    service, task_one, task_two, model = build_service(response)
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_explicit_model",
                message="按分行统计新增客户数",
                preferred_scene=PreferredScene.SQL_GENERATION,
                context={"sqlDialect": "HIVE_SQL", "schemaScope": ["card_dm"]},
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert task_one.requests == []
    assert len(task_two.requests) == 1
    sent = task_two.requests[0]
    assert sent.requirement.metric == ["新增客户数"]
    assert sent.requirement.dimensions == ["分行"]
    assert sent.execution_context.sql_dialect == "HIVE_SQL"
    assert sent.execution_context.schema_scope == ["card_dm"]
    assert {item["function"]["name"] for item in model.tools} == {"generate_sql"}
    assert "tool_choice" not in model.bind_options


def test_auto_scene_uses_single_model_selected_tool() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "generate_sql",
                "args": {
                    "requirement": {"metric": ["月活客户数"]},
                    "executionContext": {"sqlDialect": "HIVE_SQL"},
                },
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    service, task_one, task_two, model = build_service(response)
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_auto",
                message="生成各分行月活 Hive SQL",
            )
        )
    )

    assert result.scene.value == "SQL_GENERATION"
    assert task_one.requests == []
    assert len(task_two.requests) == 1
    assert {item["function"]["name"] for item in model.tools} == {
        "check_asset_duplicate",
        "generate_sql",
    }
    assert "tool_choice" not in model.bind_options


def test_incomplete_asset_requirement_is_clarified_before_tool_execution() -> None:
    service, task_one, task_two, _ = build_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_asset_missing",
                message="帮我检查重复资产",
                preferred_scene=PreferredScene.ASSET_DUPLICATE,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "CLARIFICATION"
    assert result.required_information == ["draftAsset.assetDescription"]
    assert "名称" in result.result["question"]
    assert task_one.requests == []
    assert task_two.requests == []


def test_incomplete_sql_requirement_is_clarified_before_tool_execution() -> None:
    service, task_one, task_two, _ = build_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_sql_missing",
                message="帮我生成 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "CLARIFICATION"
    assert result.required_information == ["requirement.metric"]
    assert "指标" in result.result["question"]
    assert task_one.requests == []
    assert task_two.requests == []
    assert result.execution_plan.status.value == "WAITING_INPUT"
    assert result.execution_plan.selected_tool.value == "generate_sql"
    assert result.execution_plan.constraints.professional_tool_calls == 0
    assert all(
        step.code.value != "EXECUTE"
        for step in result.execution_plan.steps
    )


def test_context_dependent_fallback_clarifies_instead_of_guessing() -> None:
    service, task_one, task_two, _ = build_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_context_fallback",
                message="改成180天，沿用刚才的表",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.required_information == ["message"]
    assert "无法安全解析" in result.summary
    assert task_one.requests == []
    assert task_two.requests == []


def test_downstream_need_more_information_is_returned_to_user() -> None:
    service, _, task_two, _ = build_service(
        task_two_status="NEED_MORE_INFORMATION"
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_downstream_clarify",
                message="生成新增客户数 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert len(task_two.requests) == 1
    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.result["kind"] == "clarification"
    assert result.required_information == ["requirement.metric"]


def test_processing_from_synchronous_downstream_is_rejected() -> None:
    service, _, task_two, _ = build_service(task_two_status="PROCESSING")
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_processing",
                message="生成新增客户数 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert len(task_two.requests) == 1
    assert result.status == ResultStatus.FAILED
    assert result.error.code == "DOWNSTREAM_INVALID_STATE"
    assert result.error.retryable is True
    assert result.execution_plan.status.value == "FAILED"
    assert result.execution_plan.steps[-1].code.value == "INSPECT"
    assert result.execution_plan.steps[-1].status.value == "FAILED"


def test_out_of_scope_request_returns_clarification() -> None:
    service, task_one, task_two, _ = build_service(AIMessage(content="请明确研发任务。"))
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_other",
                message="帮我写一首诗",
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "CLARIFICATION"
    assert result.result["kind"] == "clarification"
    assert task_one.requests == []
    assert task_two.requests == []


def test_unknown_model_tool_is_rejected_without_professional_call() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "unknown_professional_tool",
                "args": {},
                "id": "call_unknown",
                "type": "tool_call",
            }
        ],
    )
    service, task_one, task_two, _ = build_service(response)

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_unknown_tool",
                message="执行未知能力",
            )
        )
    )

    assert result.status == ResultStatus.FAILED
    assert result.execution_plan.selected_tool is None
    assert result.execution_plan.constraints.professional_tool_calls == 0
    assert task_one.requests == []
    assert task_two.requests == []


def test_multiple_model_tool_calls_fall_back_to_one_professional_tool() -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "check_asset_duplicate",
                "args": {"draftAsset": {"assetDescription": "客户资产"}},
                "id": "call_asset",
                "type": "tool_call",
            },
            {
                "name": "generate_sql",
                "args": {"requirement": {"metric": ["新增客户数"]}},
                "id": "call_sql",
                "type": "tool_call",
            },
        ],
    )
    service, task_one, task_two, _ = build_service(response)

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_multi_tool",
                message="生成新增客户数SQL",
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert task_one.requests == []
    assert len(task_two.requests) == 1
    assert result.execution_plan.selected_tool.value == "generate_sql"
    assert result.execution_plan.constraints.professional_tool_calls == 1


def test_plan_and_route_tool_mismatch_is_rejected_before_execution() -> None:
    service, task_one, task_two, _ = build_service(
        execution_plan_builder=CrossScenePlanBuilder()
    )

    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_plan_mismatch",
                message="生成新增客户数SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.FAILED
    assert result.execution_plan.constraints.professional_tool_calls == 0
    assert result.execution_plan.constraints.allow_cross_scene is False
    assert task_one.requests == []
    assert task_two.requests == []

