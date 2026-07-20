import os
import time

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

        complex_query = client.post(
            "/api/v1/query",
            json={
                "question": "查询近30天各分行信用卡交易金额和排名。",
                "query_mode": "ontology",
            },
        )
        assert complex_query.status_code == 200, complex_query.text
        complex_result = complex_query.json()
        assert complex_result["status"] == "success"
        assert complex_result["selected_sql_asset"]["id"] == ("sqlasset-branch-credit-window")
        assert complex_result["selected_template_rank"] is not None
        assert complex_result["sql_rewrite"]["used_template"]
        assert "WITH branch_totals" in complex_result["generated_sql"]
        assert "DENSE_RANK() OVER" in complex_result["generated_sql"]
        assert complex_result["validation_report"]["valid"]
        assert complex_result["validation_report"]["explain_passed"]
        assert complex_result["execution_result"]["row_count"] > 0

        migrated_draft = client.post(
            "/api/v1/ontology/drafts/migrate-legacy",
            json={
                "draft_name": "API object manager migration",
                "created_by": "api-test",
            },
        )
        assert migrated_draft.status_code == 200, migrated_draft.text
        draft = migrated_draft.json()
        draft_id = draft["draft"]["id"]
        transaction = next(
            item for item in draft["resources"]["object_types"] if item["id"] == "transaction"
        )
        assert "transaction.amount" in transaction["property_ids"]
        binding = next(
            item
            for item in draft["resources"]["bindings"]
            if item["object_type_id"] == "transaction"
        )
        assert binding["table_name"] == "dwd_card_transaction"
        assert binding["property_bindings"]["transaction.amount"] == "txn_amount_cny"
        assert any(
            item["id"] == "transaction_belongs_to_branch"
            for item in draft["resources"]["link_types"]
        )
        draft_diff = client.get(f"/api/v1/ontology/drafts/{draft_id}/diff")
        assert draft_diff.status_code == 200, draft_diff.text
        assert draft_diff.json()["added_objects"]
        draft_impact = client.get(f"/api/v1/ontology/drafts/{draft_id}/impact")
        assert draft_impact.status_code == 200, draft_impact.text
        assert draft_impact.json()["rebuild_concept_index"]
        validated_draft = client.post(f"/api/v1/ontology/drafts/{draft_id}/validate")
        assert validated_draft.status_code == 200, validated_draft.text
        assert validated_draft.json()["draft"]["validation_report"]["valid"]
        dry_run_cases = validated_draft.json()["draft"]["validation_report"][
            "dry_run_cases"
        ]
        assert len(dry_run_cases) == 4
        assert all(item["explain_passed"] for item in dry_run_cases)
        submitted_draft = client.post(
            f"/api/v1/ontology/drafts/{draft_id}/submit",
            json={"actor": "api-author"},
        )
        assert submitted_draft.status_code == 200
        forbidden_edit = client.put(
            f"/api/v1/ontology/drafts/{draft_id}/object-types/transaction",
            json=transaction,
        )
        assert forbidden_edit.status_code == 409
        approved_draft = client.post(
            f"/api/v1/ontology/drafts/{draft_id}/approve",
            json={"actor": "api-reviewer"},
        )
        assert approved_draft.status_code == 200, approved_draft.text
        published_objects = client.post(
            f"/api/v1/ontology/drafts/{draft_id}/publish",
            json={"actor": "api-reviewer", "version": "api-object-0.1"},
        )
        assert published_objects.status_code == 200, published_objects.text
        object_graph = client.get("/api/v1/ontology/object-graph")
        assert object_graph.status_code == 200
        assert len(object_graph.json()["nodes"]) == 6
        assert any(
            item["id"] == "transaction_belongs_to_branch" for item in object_graph.json()["edges"]
        )
        published_transaction = client.get("/api/v1/ontology/object-types/transaction")
        assert published_transaction.status_code == 200
        assert "transaction.amount" in published_transaction.json()["property_ids"]

        index_build = client.post(
            "/api/v1/ontology/index-builds",
            json={"index_type": "BUSINESS_CONCEPT"},
        )
        assert index_build.status_code == 200, index_build.text
        assert index_build.json()["status"] == "READY"
        assert index_build.json()["document_count"] > 0
        assert index_build.json()["is_current"]
        sync_run = client.post("/api/v1/ontology/sync-runs")
        assert sync_run.status_code == 200, sync_run.text
        assert sync_run.json()["status"] == "READY"
        assert all(
            item["severity"] != "BREAKING" for item in sync_run.json()["reports"]
        )

        object_records = client.get("/api/v1/objects/transaction?limit=2")
        assert object_records.status_code == 200, object_records.text
        assert object_records.json()
        record = object_records.json()[0]
        assert "transaction_belongs_to_branch" in record["available_links"]
        assert "***MASKED***" in record["properties"].values()
        object_detail = client.get(
            f"/api/v1/objects/transaction/{record['primary_key']}"
        )
        assert object_detail.status_code == 200, object_detail.text
        linked_branch = client.get(
            f"/api/v1/objects/transaction/{record['primary_key']}/links/"
            "transaction_belongs_to_branch"
        )
        assert linked_branch.status_code == 200, linked_branch.text
        assert linked_branch.json()
        rejected_parameter = client.get(
            "/api/v1/objects/transaction?raw_sql=select+1"
        )
        assert rejected_parameter.status_code == 422

        object_query = client.post(
            "/api/v1/query",
            json={
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
            },
        )
        assert object_query.status_code == 200, object_query.text
        assert object_query.json()["execution_result"]["row_count"] > 0

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

        isolated_results = {}
        for mode, enabled, variant in (
            ("schema", False, "schema"),
            ("rag", False, "rag"),
            ("ontology", False, "ontology_no_sql_asset"),
            ("ontology", True, "ontology_full"),
        ):
            response = client.post(
                "/api/v1/query",
                json={
                    "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                    "query_mode": mode,
                    "sql_asset_enabled": enabled,
                },
            )
            assert response.status_code == 200, response.text
            isolated_results[variant] = response.json()
            assert response.json()["strategy_variant"] == variant
            assert response.json()["status"] == "success"
        assert isolated_results["schema"]["semantic_query"] is None
        assert isolated_results["rag"]["semantic_query"] is None
        assert isolated_results["schema"]["sql_asset_candidates"] is None
        assert isolated_results["rag"]["sql_asset_candidates"] is None
        assert isolated_results["ontology_no_sql_asset"]["selected_sql_asset"] is None
        assert isolated_results["ontology_no_sql_asset"]["sql_asset_candidates"] is None
        assert isolated_results["ontology_no_sql_asset"]["sql_rewrite"] is None
        assert isolated_results["ontology_full"]["sql_asset_candidates"]

        run_ids = []
        for mode, enabled, variant in (
            ("schema", False, "schema"),
            ("rag", False, "rag"),
            ("ontology", False, "ontology_no_sql_asset"),
            ("ontology", True, "ontology_full"),
        ):
            created = client.post(
                "/api/v1/evaluation/runs",
                json={
                    "query_mode": mode,
                    "strategy_variant": variant,
                    "sql_asset_enabled": enabled,
                    "run_kind": "smoke",
                    "max_cases": 2,
                    "concurrency": 1,
                },
            )
            assert created.status_code == 202, created.text
            run_ids.append(created.json()["run_id"])
        for run_id in run_ids:
            for _ in range(20):
                state = client.get(f"/api/v1/evaluation/runs/{run_id}")
                assert state.status_code == 200, state.text
                if state.json()["run"]["status"] == "COMPLETED":
                    break
                time.sleep(0.05)
            assert state.json()["run"]["status"] == "COMPLETED"
            cases = client.get(f"/api/v1/evaluation/runs/{run_id}/cases")
            assert len(cases.json()) == 2
        comparison = client.get(
            "/api/v1/evaluation/compare",
            params=[("run_id", run_id) for run_id in run_ids],
        )
        assert comparison.status_code == 200, comparison.text
        assert len(comparison.json()["runs"]) == 4
        exported = client.get(
            f"/api/v1/evaluation/runs/{run_ids[0]}/export",
            params={"format": "csv"},
        )
        assert exported.status_code == 200
        assert "case_id" in exported.text

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
            item for item in mappings if item["payload"]["candidate_concept_id"] == concept["id"]
        )
        verified_mapping = client.post(
            f"/api/v1/ontology/candidates/{mapping['id']}/verify",
            json={"reviewer": "api-test", "note": "mapping verified", "edits": {}},
        )
        assert verified_mapping.status_code == 200

        rejected_concept = next(item for item in candidates.json() if item["id"] != concept["id"])
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
        credit_metric = next(item for item in hybrid.json() if item["name"] == "信用卡交易金额")
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

        activated = client.post("/api/v1/ontology/versions/api-test-0.2/activate")
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
