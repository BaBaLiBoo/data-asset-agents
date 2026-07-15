import os

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 with an initialized PostgreSQL database",
)


def test_fastapi_health_query_parse_resolve_and_unsupported() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "up"}

        parsed = client.post(
            "/api/v1/semantic/parse",
            json={
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
            },
        )
        assert parsed.status_code == 200
        semantic_query = parsed.json()
        assert semantic_query["metric_names"] == ["信用卡交易金额", "信用卡交易笔数"]

        resolved = client.post(
            "/api/v1/semantic/resolve",
            json={"semantic_query": semantic_query},
        )
        assert resolved.status_code == 200
        assert resolved.json()["selected_tables"] == [
            "dwd_card_transaction",
            "dim_branch",
        ]

        query = client.post(
            "/api/v1/query",
            json={
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
            },
        )
        assert query.status_code == 200
        assert query.json()["status"] == "success"
        assert query.json()["execution_result"]["row_count"] > 0

        unsupported = client.post(
            "/api/v1/query",
            json={"question": "查询明天的天气", "query_mode": "ontology"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["status"] == "unsupported"
        assert unsupported.json()["error_code"] == "UNSUPPORTED_QUERY"

