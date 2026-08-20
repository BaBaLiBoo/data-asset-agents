"""结果反思智能体的结构化输出和安全边界测试。"""

import asyncio
from typing import Any

import pytest

from asset_supervisor.agents.result_reflection_agent import (
    MAX_RESULT_CHARS,
    ResultReflectionAgent,
    ResultReflectionError,
)
from asset_supervisor.models.api import AgentScene
from asset_supervisor.models.memory import ConversationMemory
from asset_supervisor.models.reflection import (
    ReflectionCategory,
    ReflectionItem,
    ReflectionItemStatus,
    ReflectionRiskLevel,
    ReflectionStatus,
    ResultReflection,
)


class FakeStructuredModel:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.schema = None
        self.structured_options = {}
        self.invocations = []

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.structured_options = kwargs
        return self

    async def ainvoke(self, messages):
        self.invocations.append(messages)
        if self.error is not None:
            raise self.error
        return self.response


def warning_reflection(*, status: ReflectionStatus) -> ResultReflection:
    return ResultReflection(
        status=status,
        summary="时间范围需要人工确认",
        risk_level=ReflectionRiskLevel.MEDIUM,
        items=[
            ReflectionItem(
                category=ReflectionCategory.TIME_RANGE,
                status=ReflectionItemStatus.WARNING,
                requirement="最近180天",
                observation="SQL使用最近90天",
                suggestion="确认后按180天重新生成",
            )
        ],
    )


def test_reflection_uses_structured_output_without_binding_professional_tools() -> None:
    model = FakeStructuredModel(
        warning_reflection(status=ReflectionStatus.PASSED)
    )
    agent = ResultReflectionAgent(model)

    result = asyncio.run(
        agent.reflect(
            original_query="改成最近180天",
            scene=AgentScene.SQL_GENERATION,
            structured_arguments={"requirement": {"metric": ["新增客户数"]}},
            professional_result={
                "kind": "sql",
                "sql": "INTERVAL 90 DAY",
                "recommendedTables": [{"tableName": "customer_account"}],
                "recommendedFields": [
                    {"tableName": "customer_account", "fieldName": "open_date"}
                ],
            },
            conversation_memory=ConversationMemory(
                prompt="最近需求：沿用客户主题表",
                message_count=2,
                char_count=12,
            ),
        )
    )

    assert model.schema is ResultReflection
    assert model.structured_options == {"method": "function_calling"}
    assert result.status is ReflectionStatus.WARNING
    assert len(model.invocations) == 1
    messages = model.invocations[0]
    assert len(messages) == 2
    assert "不得请求或调用任何Tool" in messages[0].content
    assert "当前场景：SQL_GENERATION" in messages[0].content
    assert "最近180天" in messages[1].content
    assert "沿用客户主题表" in messages[1].content
    assert "selectedTables" in messages[1].content
    assert "selectedFields" in messages[1].content
    assert "recommendedTables" not in messages[1].content
    assert "recommendedFields" not in messages[1].content


def test_sql_reflection_removes_asset_reuse_content() -> None:
    model = FakeStructuredModel(
        ResultReflection(
            status=ReflectionStatus.WARNING,
            summary="发现候选资产，建议优先复用存量资产",
            risk_level=ReflectionRiskLevel.MEDIUM,
            items=[
                ReflectionItem(
                    category=ReflectionCategory.ASSET_EVIDENCE,
                    status=ReflectionItemStatus.WARNING,
                    requirement="检查资产查重结果",
                    observation="候选资产达到召回阈值",
                    suggestion="建议复用候选资产",
                ),
                ReflectionItem(
                    category=ReflectionCategory.TABLE_FIELD,
                    status=ReflectionItemStatus.PASSED,
                    requirement="使用客户开户日期字段",
                    observation="SQL使用customer_account.open_date",
                    suggestion="",
                ),
            ],
        )
    )

    result = asyncio.run(
        ResultReflectionAgent(model).reflect(
            original_query="统计最近30天新增客户数",
            scene=AgentScene.SQL_GENERATION,
            structured_arguments={"requirement": {"metric": ["新增客户数"]}},
            professional_result={"kind": "sql", "sql": "SELECT 1"},
        )
    )

    assert result.status is ReflectionStatus.PASSED
    assert result.summary == "SQL结果的需求覆盖检查通过。"
    assert [item.category for item in result.items] == [
        ReflectionCategory.TABLE_FIELD
    ]
    assert "复用" not in result.model_dump_json()
    assert "查重" not in result.model_dump_json()


def test_asset_reflection_keeps_asset_evidence_only() -> None:
    model = FakeStructuredModel(
        ResultReflection(
            status=ReflectionStatus.WARNING,
            summary="候选证据需要人工确认",
            risk_level=ReflectionRiskLevel.MEDIUM,
            items=[
                ReflectionItem(
                    category=ReflectionCategory.ASSET_EVIDENCE,
                    status=ReflectionItemStatus.UNKNOWN,
                    requirement="检查候选的语义和血缘证据",
                    observation="血缘证据不足",
                    suggestion="补充血缘证据后人工确认",
                ),
                ReflectionItem(
                    category=ReflectionCategory.TIME_RANGE,
                    status=ReflectionItemStatus.PASSED,
                    requirement="最近30天",
                    observation="时间范围一致",
                    suggestion="",
                ),
            ],
        )
    )

    result = asyncio.run(
        ResultReflectionAgent(model).reflect(
            original_query="检查是否存在可复用资产",
            scene=AgentScene.ASSET_DUPLICATE,
            structured_arguments={},
            professional_result={"kind": "duplicate", "candidates": []},
        )
    )

    assert [item.category for item in result.items] == [
        ReflectionCategory.ASSET_EVIDENCE
    ]


def test_reflection_wraps_model_failure() -> None:
    agent = ResultReflectionAgent(
        FakeStructuredModel(error=RuntimeError("model unavailable"))
    )

    with pytest.raises(ResultReflectionError, match="结果反思失败"):
        asyncio.run(
            agent.reflect(
                original_query="生成SQL",
                scene=AgentScene.SQL_GENERATION,
                structured_arguments={},
                professional_result={"kind": "sql", "sql": "SELECT 1"},
            )
        )


def test_reflection_bounds_large_professional_result() -> None:
    model = FakeStructuredModel(
        ResultReflection(
            status=ReflectionStatus.PASSED,
            summary="检查通过",
            risk_level=ReflectionRiskLevel.LOW,
            items=[],
        )
    )
    agent = ResultReflectionAgent(model)

    asyncio.run(
        agent.reflect(
            original_query="生成SQL",
            scene=AgentScene.SQL_GENERATION,
            structured_arguments={},
            professional_result={"sql": "X" * (MAX_RESULT_CHARS + 1000)},
        )
    )

    assert "…[TRUNCATED]" in model.invocations[0][1].content
