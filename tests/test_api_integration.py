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
        assert query.json()["sql_asset_candidates"]
        assert query.json()["selected_sql_asset"]["certified"]

        built_assets = client.post("/api/v1/sql-assets/build", json={})
        assert built_assets.status_code == 200, built_assets.text
        assert built_assets.json()["eligible"] >= 3
        assets = client.get("/api/v1/sql-assets")
        assert assets.status_code == 200
        assert len(assets.json()) >= 6
        asset_id = assets.json()[0]["id"]
        asset_detail = client.get(f"/api/v1/sql-assets/{asset_id}")
        assert asset_detail.status_code == 200
        assert asset_detail.json()["ast_fingerprint"]
        asset_search = client.post(
            "/api/v1/sql-assets/search",
            json={"question": "查询各分行信用卡交易金额", "limit": 3},
        )
        assert asset_search.status_code == 200, asset_search.text
        assert asset_search.json()
        assert asset_search.json()[0]["score"]["total"] > 0
        assert asset_search.json()[0]["asset"]["lifecycle_valid"]

        unsupported = client.post(
            "/api/v1/query",
            json={"question": "查询明天的天气", "query_mode": "ontology"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["status"] == "unsupported"
        assert unsupported.json()["error_code"] == "UNSUPPORTED_QUERY"

        clarification = client.post(
            "/api/v1/query",
            json={"question": "查询交易情况", "query_mode": "ontology"},
        )
        assert clarification.status_code == 200
        assert clarification.json()["status"] == "clarification_required"

        build = client.post(
            "/api/v1/ontology/build",
            json={
                "schema_name": "public",
                "tables": ["dim_branch"],
                "sample_limit": 3,
                "top_value_limit": 3,
            },
        )
        assert build.status_code == 200, build.text
        build_result = build.json()
        assert build_result["snapshot"]["tables"][0]["primary_key"] == ["branch_id"]
        assert build_result["concepts"][0]["status"] == "CANDIDATE"

        candidates = client.get(
            "/api/v1/ontology/candidates",
            params={
                "status_filter": "CANDIDATE",
                "candidate_type": "concept",
                "snapshot_id": build_result["snapshot"]["id"],
            },
        )
        assert candidates.status_code == 200
        concept = candidates.json()[0]
        detail = client.get(f"/api/v1/ontology/candidates/{concept['id']}")
        assert detail.status_code == 200

        verified = client.post(
            f"/api/v1/ontology/candidates/{concept['id']}/verify",
            json={
                "reviewer": "api-test",
                "note": "verified in integration test",
                "edits": {"business_name": "审核后的字段名称"},
            },
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == "VERIFIED"

        mappings = client.get(
            "/api/v1/ontology/candidates",
            params={
                "status_filter": "CANDIDATE",
                "candidate_type": "mapping",
                "snapshot_id": build_result["snapshot"]["id"],
            },
        ).json()
        mapping = next(
            item
            for item in mappings
            if item["payload"]["candidate_concept_id"] == concept["id"]
        )
        verified_mapping = client.post(
            f"/api/v1/ontology/candidates/{mapping['id']}/verify",
            json={"reviewer": "api-test", "note": "mapping verified", "edits": {}},
        )
        assert verified_mapping.status_code == 200

        rejected_concept = next(
            item
            for item in candidates.json()
            if item["id"] != concept["id"]
        )
        rejected = client.post(
            f"/api/v1/ontology/candidates/{rejected_concept['id']}/reject",
            json={"reviewer": "api-test", "note": "not accepted", "edits": {}},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "REJECTED"

        publish_payload = {
            "version": "api-test-0.2",
            "description": "API integration ontology",
            "published_by": "api-test",
            "snapshot_id": build_result["snapshot"]["id"],
        }
        dry_run = client.post(
            "/api/v1/ontology/publish/validate",
            json=publish_payload,
        )
        assert dry_run.status_code == 200, dry_run.text
        assert dry_run.json()["valid"]
        assert dry_run.json()["sqlglot_valid"]
        assert dry_run.json()["explain_passed"]

        published = client.post(
            "/api/v1/ontology/publish",
            json=publish_payload,
        )
        assert published.status_code == 200, published.text
        assert published.json()["version"] == "api-test-0.2"
        versions = client.get("/api/v1/ontology/versions")
        assert versions.status_code == 200
        assert versions.json()[0]["is_current"]

        hybrid = client.post(
            "/api/v1/semantic/search",
            json={"query": "消费金额", "limit": 5},
        )
        assert hybrid.status_code == 200
        credit_metric = next(
            item for item in hybrid.json() if item["name"] == "信用卡交易金额"
        )
        assert credit_metric["synonym_score"] == 1
        assert credit_metric["evidence"]

        second_payload = {
            **publish_payload,
            "version": "api-test-0.3",
            "description": "second API integration ontology",
        }
        second = client.post("/api/v1/ontology/publish", json=second_payload)
        assert second.status_code == 200, second.text
        assert second.json()["is_current"]

        activated = client.post(
            "/api/v1/ontology/versions/api-test-0.2/activate"
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["version"] == "api-test-0.2"
        assert activated.json()["is_current"]

        query_after_publish = client.post(
            "/api/v1/query",
            json={
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
            },
        )
        assert query_after_publish.status_code == 200, query_after_publish.text
        assert query_after_publish.json()["execution_result"]["row_count"] > 0
