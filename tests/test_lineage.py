"""任务三算子级血缘解析的路由、异步受理与流式进度测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from asset_supervisor.models.api import (
    ChatRequest,
    PreferredScene,
    ResultStatus,
)
from asset_supervisor.services.supervisor import SupervisorService
from asset_supervisor.tools.registry import ToolRegistry
from asset_supervisor.tools.task_tools import AnalyzeLineageTool


class FakeLineageClient:
    """模拟血缘专业服务的上传、任务创建与状态查询。"""

    def __init__(self, *, complete_after_calls: int = 0) -> None:
        self.complete_after_calls = complete_after_calls
        self.get_calls = 0
        self.created: list[Any] = []

    async def create_job(self, request) -> dict[str, Any]:
        self.created.append(request)
        return self._job(request, completed=False)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        self.get_calls += 1
        completed = self.get_calls > self.complete_after_calls
        request = self.created[-1]
        return self._job(request, completed=completed)

    async def upload_script(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "scriptId": "script_test",
            "fileName": payload["fileName"],
            "sizeBytes": len(payload["content"]),
            "dialect": payload.get("dialect"),
            "uploadedAt": "2026-07-30T10:00:00+00:00",
        }

    async def get_graph(self, job_id: str, *, cursor=None) -> dict[str, Any]:
        return {
            "nodes": [],
            "edges": [],
            "paths": [],
            "nextCursor": None,
            "hasMore": False,
            "totalNodes": 0,
            "totalEdges": 0,
            "totalPaths": 0,
        }

    @staticmethod
    def _job(request, *, completed: bool) -> dict[str, Any]:
        status = "COMPLETED" if completed else "PENDING"
        return {
            "jobId": "job_test_1",
            "requestId": request.request_id,
            "taskId": request.task_id,
            "traceId": request.trace_id,
            "scriptId": request.script_id,
            "targetField": request.target_field,
            "status": status,
            "progress": 100 if completed else 0,
            "stages": [
                {"code": "PREPROCESSING", "label": "脚本预处理", "status": "SUCCESS"},
                {"code": "PARSING", "label": "语法解析", "status": "SUCCESS"},
            ],
            "result": FakeLineageClient._result() if completed else None,
            "error": None,
            "createdAt": "2026-07-30T10:00:00+00:00",
            "updatedAt": "2026-07-30T10:00:01+00:00",
        }

    @staticmethod
    def _result() -> dict[str, Any]:
        return {
            "kind": "lineage",
            "jobId": "job_test_1",
            "scriptId": "script_test",
            "targetField": "EAST.BD_ODS_BNWYWDBHTB::DBHTH",
            "sqlDialect": "Oracle",
            "metrics": {
                "sourceTables": 38,
                "sourceFields": 62,
                "sourcePaths": 13,
                "maxNestingDepth": 5,
                "operatorCount": 126,
                "joinCount": 21,
                "caseCount": 17,
            },
            "paths": [],
            "graph": None,
            "graphSummary": {"totalNodes": 126, "totalEdges": 131, "totalPaths": 13},
            "summary": "已完成 DBHTH 字段算子级血缘解析。",
            "confidence": 0.94,
            "warnings": [],
        }


def build_lineage_service(
    client: FakeLineageClient | None = None,
) -> tuple[SupervisorService, FakeLineageClient]:
    client = client or FakeLineageClient()
    tool = AnalyzeLineageTool(client)
    registry = ToolRegistry([tool])
    service = SupervisorService(
        None,
        registry,
        lineage_client=client,
    )
    return service, client


def lineage_request(**context: str | None) -> ChatRequest:
    return ChatRequest(
        conversation_id="conv_lineage",
        message="解析 EAST.BD_ODS_BNWYWDBHTB::DBHTH 字段的血缘",
        preferred_scene=PreferredScene.LINEAGE_PARSING,
        context={
            "scriptId": context.get("scriptId", "script_test"),
            "targetField": context.get(
                "targetField",
                "EAST.BD_ODS_BNWYWDBHTB::DBHTH",
            ),
            "sqlDialect": context.get("sqlDialect", "Oracle"),
        },
    )


def test_lineage_sync_returns_accepted_with_job_id() -> None:
    service, client = build_lineage_service()
    result = asyncio.run(service.handle(lineage_request()))

    assert result.status is ResultStatus.PROCESSING
    assert result.scene.value == "LINEAGE_PARSING"
    assert result.result["kind"] == "lineage"
    assert result.result["jobId"] == "job_test_1"
    assert len(client.created) == 1
    assert result.execution_plan.selected_tool.value == "parse_regulatory_lineage"


def test_lineage_missing_script_is_clarified_before_job_creation() -> None:
    service, client = build_lineage_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_lineage_missing",
                message="解析目标字段血缘",
                preferred_scene=PreferredScene.LINEAGE_PARSING,
            )
        )
    )

    assert result.status is ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "CLARIFICATION"
    assert "scriptId" in result.required_information
    assert "targetField" in result.required_information
    assert client.created == []


def test_lineage_stream_completes_with_review_required() -> None:
    service, client = build_lineage_service(
        FakeLineageClient(complete_after_calls=0)
    )

    async def collect() -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        async for name, data in service.stream_chat(lineage_request()):
            events.append((name, data))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]

    assert names[0] == "task"
    assert "result" in names
    assert names[-1] == "done"

    result_event = next(data for name, data in events if name == "result")
    assert result_event["scene"] == "LINEAGE_PARSING"
    assert result_event["status"] == "REVIEW_REQUIRED"
    assert result_event["result"]["kind"] == "lineage"
    assert result_event["result"]["metrics"]["sourcePaths"] == 13

    done_event = next(data for name, data in events if name == "done")
    assert done_event["status"] == "REVIEW_REQUIRED"


def test_lineage_keyword_routes_without_model() -> None:
    service, client = build_lineage_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_lineage_keyword",
                message="追溯 DBHTH 的来源路径与加工算子",
                preferred_scene=PreferredScene.AUTO,
                context={
                    "scriptId": "script_test",
                    "targetField": "EAST.BD_ODS_BNWYWDBHTB::DBHTH",
                },
            )
        )
    )

    assert result.scene.value == "LINEAGE_PARSING"
    assert result.status is ResultStatus.PROCESSING
    assert result.result["kind"] == "lineage"
    assert len(client.created) == 1
