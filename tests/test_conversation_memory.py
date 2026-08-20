"""数据库会话记忆提取、注入和多轮续接测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from asset_supervisor.agents.requirement_agent import RequirementUnderstandingAgent
from asset_supervisor.agents.task_agents import TaskAgentPort
from asset_supervisor.api import create_app, get_supervisor, get_task_repository
from asset_supervisor.models.api import ConversationMessageResponse
from asset_supervisor.repositories import SqliteTaskRepository
from asset_supervisor.services.conversation_memory import ConversationMemoryBuilder
from asset_supervisor.services.supervisor import SupervisorService
from asset_supervisor.tools.registry import ToolRegistry
from asset_supervisor.tools.task_tools import AssetDuplicateCheckTool, GenerateSqlTool


class ScriptedToolCallingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = responses
        self.invocations: list[list[Any]] = []
        self.bound_tools: list[list[dict[str, Any]]] = []

    def bind_tools(self, tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        self.bound_tools.append(list(tools))
        return self

    async def ainvoke(self, messages):
        self.invocations.append(list(messages))
        index = len(self.invocations) - 1
        return self._responses[index]


class RecordingSqlAgent(TaskAgentPort):
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "requestId": request.request_id,
            "taskId": request.task_id,
            "traceId": request.trace_id,
            "resultId": f"result_{request.task_id}_v1",
            "scene": "SQL_GENERATION",
            "status": "REVIEW_REQUIRED",
            "version": 1,
            "summary": "已生成 SQL，并推荐客户主题表",
            "confidence": 0.95,
            "requiredInformation": [],
            "result": {
                "kind": "sql",
                "template": None,
                "sqlDialect": "HIVE_SQL",
                "sql": "SELECT branch_id, COUNT(DISTINCT customer_id) FROM card_dm.card_customer GROUP BY branch_id",
                "recommendedTables": [
                    {
                        "tableName": "card_dm.card_customer",
                        "reason": "客户统计主表",
                        "confidence": 0.96,
                    }
                ],
                "recommendedFields": [
                    {
                        "tableName": "card_dm.card_customer",
                        "fieldName": "customer_id",
                        "role": "METRIC_SOURCE",
                        "reason": "客户去重",
                    },
                    {
                        "tableName": "card_dm.card_customer",
                        "fieldName": "branch_id",
                        "role": "DIMENSION",
                        "reason": "分行维度",
                    },
                ],
                "explanation": "使用客户主题表按分行统计新增客户",
                "assumptions": ["新增客户按首次开户日期统计"],
                "validation": {
                    "passed": True,
                    "riskLevel": "LOW",
                    "checks": [],
                    "warnings": [],
                },
            },
            "provenance": {
                "serviceVersion": "memory-test-1.0",
                "modelVersion": "memory-test-model",
                "promptVersion": "memory-test-prompt",
                "metadataVersion": "memory-test-meta",
            },
            "latencyMs": 1,
            "error": None,
        }


class UnexpectedAssetAgent(TaskAgentPort):
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request) -> dict[str, Any]:
        self.requests.append(request)
        raise AssertionError("多轮 SQL 场景不应调用资产查重 Tool")


class NeverCalledSupervisor:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("已删除会话必须在调用 Supervisor 前被拦截")


def tool_call(arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "generate_sql",
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def sql_arguments(
    *,
    days: int,
    available_tables: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "requirement": {
            "metric": ["新增客户数"],
            "dimensions": ["分行"],
            "timeRange": {"relativeExpression": f"最近{days}天"},
        },
        "executionContext": {
            "sqlDialect": "HIVE_SQL",
            "availableTableIds": available_tables or [],
        },
    }


def build_memory_client(
    responses: list[AIMessage],
) -> tuple[TestClient, SqliteTaskRepository, ScriptedToolCallingModel, RecordingSqlAgent]:
    model = ScriptedToolCallingModel(responses)
    sql_agent = RecordingSqlAgent()
    asset_agent = UnexpectedAssetAgent()
    registry = ToolRegistry(
        [
            AssetDuplicateCheckTool(asset_agent),
            GenerateSqlTool(sql_agent),
        ]
    )
    supervisor = SupervisorService(
        RequirementUnderstandingAgent(model, registry.llm_definitions()),
        registry,
    )
    repository = SqliteTaskRepository(":memory:")
    app = create_app()
    app.dependency_overrides[get_supervisor] = lambda: supervisor
    app.dependency_overrides[get_task_repository] = lambda: repository
    return TestClient(app), repository, model, sql_agent


def post_chat(
    client: TestClient,
    conversation_id: str,
    message: str,
    *,
    user_id: str = "u_memory",
) -> Any:
    return client.post(
        "/api/v1/chat",
        json={
            "conversationId": conversation_id,
            "message": message,
            "preferredScene": "AUTO",
            "context": {"sqlDialect": "HIVE_SQL"},
        },
        headers={"X-User-Id": user_id},
    )


def test_memory_builder_extracts_business_summary_without_full_sql() -> None:
    messages = [
        ConversationMessageResponse(
            message_id="m1",
            conversation_id="conv_memory",
            role="USER",
            content="生成近90天各分行新增客户数SQL",
            sequence_number=1,
            task_id="task_1",
            metadata={"preferredScene": "SQL_GENERATION", "context": {}},
            created_at="2026-07-29T10:00:00+08:00",
        ),
        ConversationMessageResponse(
            message_id="m2",
            conversation_id="conv_memory",
            role="ASSISTANT",
            content="已生成 SQL",
            sequence_number=2,
            task_id="task_1",
            result_id="result_1",
            metadata={
                "response": {
                    "scene": "SQL_GENERATION",
                    "status": "REVIEW_REQUIRED",
                    "summary": "已生成 SQL",
                    "requiredInformation": [],
                    "result": {
                        "kind": "sql",
                        "sql": "SELECT secret_full_sql FROM hidden_table",
                        "recommendedTables": [
                            {"tableName": "card_dm.card_customer"}
                        ],
                        "recommendedFields": [
                            {
                                "tableName": "card_dm.card_customer",
                                "fieldName": "customer_id",
                            }
                        ],
                    },
                }
            },
            created_at="2026-07-29T10:00:01+08:00",
        ),
    ]

    memory = ConversationMemoryBuilder().build(messages)

    assert "生成近90天各分行新增客户数SQL" in memory.prompt
    assert "scene=SQL_GENERATION" in memory.prompt
    assert "tables:card_dm.card_customer" in memory.prompt
    assert "card_dm.card_customer.customer_id" in memory.prompt
    assert "secret_full_sql" not in memory.prompt


def test_memory_builder_limits_message_count_and_characters() -> None:
    messages = [
        ConversationMessageResponse(
            message_id=f"m{index}",
            conversation_id="conv_limit",
            role="USER",
            content=f"第{index}条需求 " + ("很长的业务描述" * 40),
            sequence_number=index,
            metadata={"preferredScene": "AUTO"},
            created_at="2026-07-29T10:00:00+08:00",
        )
        for index in range(1, 11)
    ]

    memory = ConversationMemoryBuilder(
        max_messages=4,
        max_chars=500,
        max_item_chars=180,
    ).build(messages)

    assert memory.message_count <= 4
    assert memory.char_count <= 500
    assert "第1条需求" not in memory.prompt
    assert "第10条需求" in memory.prompt


def test_same_conversation_inherits_requirement_and_previous_table() -> None:
    responses = [
        tool_call(sql_arguments(days=90), "call_90"),
        tool_call(
            sql_arguments(
                days=180,
                available_tables=["card_dm.card_customer"],
            ),
            "call_180",
        ),
        tool_call(sql_arguments(days=30), "call_new_conversation"),
    ]
    client, repository, model, sql_agent = build_memory_client(responses)

    first = post_chat(
        client,
        "conv_followup",
        "生成近90天各分行新增客户数SQL",
    )
    second = post_chat(
        client,
        "conv_followup",
        "改成180天，沿用刚才的表",
    )
    new_conversation = post_chat(
        client,
        "conv_new",
        "生成最近30天新增客户数SQL",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert new_conversation.status_code == 200
    assert len(sql_agent.requests) == 3
    followup_request = sql_agent.requests[1]
    assert followup_request.requirement.metric == ["新增客户数"]
    assert followup_request.requirement.dimensions == ["分行"]
    assert (
        followup_request.requirement.time_range.relative_expression
        == "最近180天"
    )
    assert followup_request.execution_context.available_table_ids == [
        "card_dm.card_customer"
    ]

    followup_prompt = "\n".join(
        str(message.content) for message in model.invocations[1]
    )
    assert "生成近90天各分行新增客户数SQL" in followup_prompt
    assert "tables:card_dm.card_customer" in followup_prompt

    new_prompt = "\n".join(
        str(message.content) for message in model.invocations[2]
    )
    assert "生成近90天各分行新增客户数SQL" not in new_prompt
    assert "card_dm.card_customer" not in new_prompt
    repository.close()


def test_clarification_followup_continues_in_same_conversation() -> None:
    responses = [
        tool_call({"requirement": {"metric": []}}, "call_clarify"),
        tool_call(sql_arguments(days=90), "call_complete"),
    ]
    client, repository, model, sql_agent = build_memory_client(responses)

    clarification = post_chat(client, "conv_clarify", "帮我生成SQL")
    completed = post_chat(
        client,
        "conv_clarify",
        "新增客户数，按分行统计，最近90天",
    )

    assert clarification.status_code == 200
    assert clarification.json()["status"] == "NEED_MORE_INFORMATION"
    assert completed.status_code == 200
    assert completed.json()["status"] == "REVIEW_REQUIRED"
    assert len(sql_agent.requests) == 1

    followup_prompt = "\n".join(
        str(message.content) for message in model.invocations[1]
    )
    assert "帮我生成SQL" in followup_prompt
    assert "required=requirement.metric" in followup_prompt
    assert "请补充需要计算的指标" in followup_prompt
    repository.close()


def test_deleted_conversation_is_rejected_before_supervisor_call() -> None:
    client, repository, _, _ = build_memory_client(
        [tool_call(sql_arguments(days=90), "call_before_delete")]
    )
    created = post_chat(client, "conv_deleted_memory", "生成近90天新增客户数SQL")
    assert created.status_code == 200
    deleted = client.delete(
        "/api/v1/conversations/conv_deleted_memory",
        headers={"X-User-Id": "u_memory"},
    )
    assert deleted.status_code == 200

    never_called = NeverCalledSupervisor()
    client.app.dependency_overrides[get_supervisor] = lambda: never_called
    rejected = post_chat(client, "conv_deleted_memory", "改成180天")

    assert rejected.status_code == 404
    assert never_called.calls == 0
    repository.close()
