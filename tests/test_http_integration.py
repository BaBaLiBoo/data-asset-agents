"""总控通过协议适配器调用 Task1 与 Text2SQL 真实接口形状。"""

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from asset_supervisor.adapters import AssetHttpTaskAgent, SqlHttpTaskAgent
from asset_supervisor.api import create_app, get_supervisor, get_task_repository
from asset_supervisor.models.api import ChatRequest, PreferredScene, ResultStatus
from asset_supervisor.repositories import SqliteTaskRepository
from asset_supervisor.services.supervisor import SupervisorService
from asset_supervisor.tools.registry import ToolRegistry
from asset_supervisor.tools.task_tools import AssetDuplicateCheckTool, GenerateSqlTool


asset_real_app = FastAPI()
sql_real_app = FastAPI()
captured_asset_requests: list[dict[str, Any]] = []
captured_sql_requests: list[dict[str, Any]] = []


@asset_real_app.post("/api/v1/task1/search-by-text")
async def task1_search(
    payload: dict[str, Any],
    x_test_scenario: str | None = Header(default=None),
) -> Any:
    captured_asset_requests.append(payload)
    if x_test_scenario == "INVALID_RESPONSE":
        return {"status": "MATCHED", "candidates": "invalid"}
    if x_test_scenario == "NO_MATCH":
        return {
            "status": "NO_MATCH",
            "retrieval_mode": "TEXT_ONLY",
            "min_score": 0.6,
            "candidates": [],
        }
    return {
        "status": "MATCHED",
        "retrieval_mode": (
            "TEXT_SQL_ENHANCED" if payload.get("sql_text") else "TEXT_ONLY"
        ),
        "min_score": 0.6,
        "candidates": [
            {
                "asset_id": "ADS_CUST_MONTH_TRADE_STAT",
                "asset_name": "客户月交易统计表",
                "description": "按客户和自然月汇总交易",
                "business_domain": "retail",
                "recall_score": 0.92,
                "score_components": {
                    "embedding": 0.94,
                    "keyword": 0.8,
                    "sql_logic": 0.89,
                },
            },
            {
                "asset_id": "LOW_SCORE",
                "asset_name": "低分候选",
                "description": "应被总控阈值过滤",
                "business_domain": "retail",
                "recall_score": 0.5,
                "score_components": {"embedding": 0.5},
            },
        ],
    }


@sql_real_app.post("/api/v1/query")
async def text2sql_query(
    payload: dict[str, Any],
    x_test_scenario: str | None = Header(default=None),
) -> Any:
    captured_sql_requests.append(payload)
    if x_test_scenario == "CLARIFICATION":
        return {
            "question": payload["question"],
            "query_mode": "ontology",
            "status": "clarification_required",
            "semantic_query": {
                "clarification_question": "请补充需要统计的业务指标。"
            },
        }
    if x_test_scenario == "UNSUPPORTED":
        return JSONResponse(
            status_code=422,
            content={"status": "unsupported", "detail": "当前本体不支持该指标。"},
        )
    if x_test_scenario == "FAILED":
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "MODEL_SERVICE_ERROR",
                "detail": "模型服务不可用",
            },
        )
    if x_test_scenario == "INVALID_RESPONSE":
        return {"status": "success", "selected_tables": []}
    return {
        "question": payload["question"],
        "query_mode": payload["query_mode"],
        "strategy_variant": "ontology_full",
        "status": "success",
        "semantic_query": {
            "metric_names": ["新增客户数"],
            "dimension_names": ["分行"],
        },
        "selected_tables": ["card_customer"],
        "selected_columns": {
            "card_customer": ["branch_id", "customer_id", "first_open_date"]
        },
        "generated_sql": (
            "SELECT branch_id, COUNT(DISTINCT customer_id) AS new_customer_count "
            "FROM card_customer WHERE first_open_date >= CURRENT_DATE - INTERVAL "
            "'90 days' GROUP BY branch_id"
        ),
        "common_validation_report": {
            "valid": True,
            "read_only": True,
            "warnings": [],
        },
        "ontology_policy_report": {"valid": True, "warnings": []},
        "validation_report": {"valid": True, "read_only": True, "warnings": []},
        "validation_errors": [],
        "execution_result": {
            "columns": ["branch_id", "new_customer_count"],
            "rows": [{"branch_id": "001", "new_customer_count": 12}],
            "row_count": 1,
        },
        "explanation": "依据已发布本体解析新增客户数和分行维度。",
        "confidence": 0.91,
        "trace_steps": [],
    }


def build_integrated_service(
    *,
    asset_headers: dict[str, str] | None = None,
    sql_headers: dict[str, str] | None = None,
    sql_transport: httpx.AsyncBaseTransport | None = None,
) -> SupervisorService:
    asset_agent = AssetHttpTaskAgent(
        base_url="http://asset.test",
        timeout_seconds=1,
        transport=httpx.ASGITransport(app=asset_real_app),
        default_headers=asset_headers,
    )
    sql_agent = SqlHttpTaskAgent(
        base_url="http://sql.test",
        timeout_seconds=1,
        transport=sql_transport or httpx.ASGITransport(app=sql_real_app),
        default_headers=sql_headers,
    )
    registry = ToolRegistry(
        [AssetDuplicateCheckTool(asset_agent), GenerateSqlTool(sql_agent)]
    )
    return SupervisorService(None, registry)


def test_supervisor_calls_task1_real_shape_and_returns_review_candidates() -> None:
    captured_asset_requests.clear()
    service = build_integrated_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_asset_http",
                message="检查客户月度交易汇总表是否有可复用资产",
                preferred_scene=PreferredScene.ASSET_DUPLICATE,
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert result.result["kind"] == "duplicate"
    assert result.result["recommendation"] == "MANUAL_REVIEW"
    assert result.result["candidates"][0]["scores"]["semantic"] == 0.94
    assert result.result["candidates"][0]["scores"]["lineage"] == 0.0
    assert len(result.result["candidates"]) == 1
    assert len(captured_asset_requests) == 1
    assert "客户月度交易汇总表" in captured_asset_requests[0]["query"]
    assert captured_asset_requests[0]["top_k"] == 10
    assert result.reflection.status.value == "UNAVAILABLE"


def test_task1_no_match_is_success_without_duplicate_claim() -> None:
    service = build_integrated_service(
        asset_headers={"X-Test-Scenario": "NO_MATCH"}
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_asset_no_match",
                message="检查客户月度交易汇总表是否有可复用资产",
                preferred_scene=PreferredScene.ASSET_DUPLICATE,
            )
        )
    )

    assert result.status == ResultStatus.SUCCESS
    assert result.result["canReuse"] is False
    assert result.result["candidates"] == []


def test_supervisor_calls_text2sql_real_shape_and_returns_sql() -> None:
    captured_sql_requests.clear()
    service = build_integrated_service()
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_sql_http",
                message="统计最近90天各分行新增客户数",
                preferred_scene=PreferredScene.SQL_GENERATION,
                context={"sqlDialect": "HIVE_SQL", "schemaScope": ["card_dm"]},
            )
        )
    )

    assert result.status == ResultStatus.REVIEW_REQUIRED
    assert result.result["kind"] == "sql"
    assert result.result["sqlDialect"] == "POSTGRESQL"
    assert "SELECT branch_id" in result.result["sql"]
    assert result.result["validation"]["passed"] is True
    assert captured_sql_requests == [
        {
            "question": "统计最近90天各分行新增客户数",
            "query_mode": "ontology",
            "sql_asset_enabled": True,
        }
    ]
    assert result.reflection.status.value == "UNAVAILABLE"


def test_incomplete_request_does_not_call_professional_service() -> None:
    service = build_integrated_service(sql_headers={"X-Test-Scenario": "FAILED"})
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_preflight",
                message="生成 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "CLARIFICATION"
    assert result.required_information == ["requirement.metric"]


def test_text2sql_clarification_is_preserved() -> None:
    service = build_integrated_service(
        sql_headers={"X-Test-Scenario": "CLARIFICATION"}
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_downstream_more",
                message="统计各分行新增客户数",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert result.scene.value == "SQL_GENERATION"
    assert result.result["kind"] == "clarification"
    assert "requirement.metric" in result.required_information
    assert result.reflection is None


def test_text2sql_unsupported_http_response_becomes_clarification() -> None:
    service = build_integrated_service(
        sql_headers={"X-Test-Scenario": "UNSUPPORTED"}
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_unsupported",
                message="统计神秘业务指标",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.NEED_MORE_INFORMATION
    assert "当前本体不支持" in result.result["question"]


def test_invalid_downstream_response_becomes_failed_result() -> None:
    service = build_integrated_service(
        asset_headers={"X-Test-Scenario": "INVALID_RESPONSE"}
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_invalid",
                message="检查客户月度交易汇总资产是否有可复用候选",
                preferred_scene=PreferredScene.ASSET_DUPLICATE,
            )
        )
    )

    assert result.status == ResultStatus.FAILED
    assert result.error.code == "DOWNSTREAM_INVALID_RESPONSE"
    assert result.result is None


def test_downstream_503_becomes_retryable_failed_result() -> None:
    service = build_integrated_service(sql_headers={"X-Test-Scenario": "FAILED"})
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_failed",
                message="生成各分行新增客户数 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.FAILED
    assert result.error.code == "MODEL_SERVICE_ERROR"
    assert result.error.retryable is True
    assert result.execution_plan.status.value == "FAILED"
    assert result.execution_plan.constraints.professional_tool_calls == 1


def test_http_timeout_becomes_downstream_timeout() -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout", request=request)

    service = build_integrated_service(
        sql_transport=httpx.MockTransport(raise_timeout)
    )
    result = asyncio.run(
        service.handle(
            ChatRequest(
                conversation_id="conv_timeout",
                message="生成各分行新增客户数 SQL",
                preferred_scene=PreferredScene.SQL_GENERATION,
            )
        )
    )

    assert result.status == ResultStatus.FAILED
    assert result.error.code == "DOWNSTREAM_TIMEOUT"
    assert result.error.retryable is True


def test_fastapi_chat_endpoint_returns_integrated_result() -> None:
    service = build_integrated_service()
    repository = SqliteTaskRepository(":memory:")
    app = create_app()
    app.dependency_overrides[get_supervisor] = lambda: service
    app.dependency_overrides[get_task_repository] = lambda: repository
    client = TestClient(app)

    response = client.post(
        "/api/v1/chat",
        json={
            "conversationId": "conv_api",
            "message": "生成新增客户数 SQL",
            "preferredScene": "SQL_GENERATION",
            "context": {
                "sqlDialect": "HIVE_SQL",
                "schemaScope": ["card_dm"],
            },
        },
        headers={"X-User-Id": "u_10086"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversationId"] == "conv_api"
    assert payload["scene"] == "SQL_GENERATION"
    assert payload["result"]["kind"] == "sql"
    repository.close()
