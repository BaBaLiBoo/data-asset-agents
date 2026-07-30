from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

WEB_PATH = Path(__file__).resolve().parents[1] / "apps" / "web"
if str(WEB_PATH) not in sys.path:
    sys.path.insert(0, str(WEB_PATH))


def test_main_navigation_has_single_product_ontology_entry() -> None:
    navigation = importlib.import_module("navigation")

    assert navigation.MAIN_NAVIGATION == [
        "演示首页",
        "本体构建与治理",
        "智能问数",
        "SQL 资产",
        "实验与评测",
        "高级管理",
    ]
    assert "Ontology Construction" not in navigation.MAIN_NAVIGATION
    assert "Ontology Manager" not in navigation.MAIN_NAVIGATION
    assert "本体构建与审核" not in navigation.MAIN_NAVIGATION


def test_demo_and_developer_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    navigation = importlib.import_module("navigation")

    monkeypatch.setenv("DEMO_MODE", "true")
    assert navigation.demo_mode_enabled() is True
    monkeypatch.setenv("DEMO_MODE", "false")
    assert navigation.demo_mode_enabled() is False


def test_session_state_keys_are_centralized() -> None:
    state = importlib.import_module("state")

    keys = {item.value for item in state.StateKey}
    assert "daa.ontology.current_draft_id" in keys
    assert "daa.ontology.draft_revision" in keys
    assert "daa.ontology.construction_run_id" in keys
    assert "manager_revision" not in keys


def test_candidate_progress_and_defer_behavior() -> None:
    state = importlib.import_module("state")

    candidates = [
        {"status": "ACCEPTED"},
        {"status": "MODIFIED"},
        {"status": "REJECTED"},
        {"status": "MERGED"},
        {"status": "DEFERRED"},
        {"status": "PENDING"},
    ]
    counts = state.candidate_counts(candidates)
    assert counts["accepted"] == 1
    assert counts["modified"] == 1
    assert counts["rejected"] == 1
    assert counts["merged"] == 1
    assert counts["deferred"] == 1
    assert counts["pending"] == 1
    assert state.completion_ratio(candidates) == pytest.approx(4 / 5)


def test_all_candidate_resource_forms_have_required_structured_fields() -> None:
    forms = importlib.import_module("components.resource_forms")

    required = {
        "object_type": {
            "id",
            "name",
            "plural_name",
            "description",
            "primary_key_property_id",
            "title_property_id",
            "lifecycle_status",
            "synonyms",
        },
        "property": {
            "id",
            "object_type_id",
            "name",
            "description",
            "data_type",
            "semantic_role",
            "nullable",
            "sensitive",
            "groupable",
            "filterable",
            "unit",
            "synonyms",
        },
        "metric": {
            "id",
            "aggregation",
            "measure_property_id",
            "filter_predicates",
            "time_property_id",
            "supported_dimension_ids",
        },
        "dimension": {"id", "name", "property_id", "time_grain", "synonyms"},
        "link_type": {
            "id",
            "name",
            "source_object_type_id",
            "target_object_type_id",
            "cardinality",
            "physical_join_ids",
        },
        "binding": {
            "id",
            "object_type_id",
            "data_source_id",
            "schema_name",
            "table_name",
            "primary_key_column",
            "property_bindings",
            "sync_status",
        },
        "physical_join": {
            "id",
            "left_table",
            "left_column",
            "right_table",
            "right_column",
            "relationship",
            "evidence",
        },
    }
    assert set(forms.RESOURCE_FORM_FIELDS) == set(required)
    for resource_type, fields in required.items():
        assert fields <= set(forms.RESOURCE_FORM_FIELDS[resource_type])


def test_stable_id_is_declared_as_read_only_in_form_source() -> None:
    source = (WEB_PATH / "components" / "resource_forms.py").read_text(encoding="utf-8")

    assert 'st.text_input("ID"' in source
    assert "disabled=True" in source


def test_modify_diff_detects_field_changes() -> None:
    diff_view = importlib.import_module("components.diff_view")

    before = {"id": "transaction_amount", "name": "交易金额"}
    after = {"id": "transaction_amount", "name": "已入账交易金额"}
    assert diff_view.changed_fields(before, after) == ["name"]
    assert "已入账交易金额" in diff_view.diff_text(before, after)


def test_resource_form_labels_cover_seven_resource_types() -> None:
    forms = importlib.import_module("components.resource_forms")

    assert forms.RESOURCE_LABELS == {
        "object_type": "ObjectType",
        "property": "Property",
        "metric": "Metric",
        "dimension": "Dimension",
        "binding": "Binding",
        "link_type": "LinkType",
        "physical_join": "PhysicalJoin",
    }


def test_workbench_endpoint_mapping_covers_resource_crud() -> None:
    workbench = importlib.import_module("pages.ontology_workbench")

    assert workbench.RESOURCE_ENDPOINTS["object_type"] == "object-types"
    assert workbench.RESOURCE_ENDPOINTS["property"] == "properties"
    assert workbench.RESOURCE_ENDPOINTS["metric"] == "metrics"
    assert workbench.RESOURCE_ENDPOINTS["dimension"] == "dimensions"
    assert workbench.RESOURCE_ENDPOINTS["link_type"] == "link-types"
    assert workbench.RESOURCE_ENDPOINTS["binding"] == "bindings"
    assert workbench.RESOURCE_ENDPOINTS["physical_join"] == "physical-joins"


def test_workbench_step_completion_uses_backend_status(monkeypatch: pytest.MonkeyPatch) -> None:
    workbench = importlib.import_module("pages.ontology_workbench")
    state = importlib.import_module("state")

    monkeypatch.setitem(workbench.st.session_state, state.StateKey.SNAPSHOT_ID.value, "snap-1")
    run = {"status": "CANDIDATES_READY"}
    candidates = [{"status": "ACCEPTED"}, {"status": "MODIFIED"}]
    draft = {"status": "DRAFT"}
    current, completed = workbench._current_step(run, candidates, draft)
    assert current == "校验发布"
    assert "数据源扫描" in completed
    assert "自动构建" in completed
    assert "候选审核" in completed
    assert "草稿完善" in completed


def test_promoted_draft_is_restored_from_run(monkeypatch: pytest.MonkeyPatch) -> None:
    workbench = importlib.import_module("pages.ontology_workbench")
    state = importlib.import_module("state")

    class FakeClient:
        def get(self, path: str) -> Any:
            if path.endswith("/construction-runs/run-1"):
                return {
                    "run_id": "run-1",
                    "status": "PROMOTED_TO_DRAFT",
                    "promoted_draft_id": "draft-1",
                }
            return []

    monkeypatch.setattr(workbench, "get_client", lambda: FakeClient())
    monkeypatch.setitem(
        workbench.st.session_state, state.StateKey.CONSTRUCTION_RUN_ID.value, "run-1"
    )
    workbench._load_run_and_candidates()
    assert workbench.st.session_state[state.StateKey.CURRENT_DRAFT_ID.value] == "draft-1"


def test_api_client_sets_if_match_and_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client = importlib.import_module("api_client")
    state = importlib.import_module("state")
    captured: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs["method"] = method
        kwargs["url"] = url
        captured.update(kwargs)
        request = httpx.Request(kwargs["method"], kwargs["url"])
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(api_client.httpx, "request", fake_request)
    monkeypatch.setitem(api_client.st.session_state, state.StateKey.DRAFT_REVISION.value, 7)
    result = api_client.ApiClient("http://api").post(
        "/api/v1/ontology/drafts/d1/object-types",
        json={"id": "customer"},
        actor="tester",
    )
    assert result == {"ok": True}
    assert captured["headers"]["If-Match"] == '"7"'
    assert captured["headers"]["X-Actor"] == "tester"


def test_api_client_reports_draft_revision_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client = importlib.import_module("api_client")

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs["method"] = method
        kwargs["url"] = url
        request = httpx.Request(kwargs["method"], kwargs["url"])
        return httpx.Response(
            409,
            json={
                "error_code": "DRAFT_REVISION_CONFLICT",
                "detail": "conflict",
                "current_revision": 8,
            },
            request=request,
        )

    monkeypatch.setattr(api_client.httpx, "request", fake_request)
    with pytest.raises(api_client.DraftRevisionConflict) as exc:
        api_client.ApiClient("http://api").post("/api/v1/ontology/drafts/d1/validate")
    assert exc.value.current_revision == 8


def test_api_client_reports_api_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client = importlib.import_module("api_client")

    def fake_request(*_: Any, **__: Any) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(api_client.httpx, "request", fake_request)
    with pytest.raises(api_client.ApiUnavailable):
        api_client.ApiClient("http://api").get("/health")


def test_query_page_has_business_process_sections() -> None:
    source = (WEB_PATH / "pages" / "intelligent_query.py").read_text(encoding="utf-8")

    for label in [
        "识别的业务指标",
        "识别的维度",
        "物理表字段映射",
        "Join Plan",
        "SQLAsset 是否命中",
        "为什么这样查询",
        "查看技术细节",
    ]:
        assert label in source


def test_demo_home_exposes_official_demo_activation() -> None:
    source = (WEB_PATH / "pages" / "demo_home.py").read_text(encoding="utf-8")

    assert "Data Asset Agent Demo" in source
    assert "启用正式演示本体" in source
    assert "MiniBank 正式业务本体 v1.0" in source
    assert "Gold" not in source


def test_legacy_pages_are_preserved_for_advanced_management() -> None:
    legacy = importlib.import_module("legacy_app")

    assert hasattr(legacy, "legacy_page")
    assert hasattr(legacy, "ontology_builder_page")
    assert hasattr(legacy, "ontology_construction_page")
    assert hasattr(legacy, "ontology_manager_page")
