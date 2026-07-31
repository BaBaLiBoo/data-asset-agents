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


def _source(path: str) -> str:
    return (WEB_PATH / path).read_text(encoding="utf-8")


def test_main_navigation_has_only_two_product_entries() -> None:
    navigation = importlib.import_module("navigation")

    assert navigation.MAIN_NAVIGATION == ["本体构建", "智能体 Demo"]
    forbidden = {
        "演示首页",
        "SQL 资产",
        "实验与评测",
        "高级管理",
        "Ontology Manager",
        "Ontology Construction Workbench",
        "本体语义层离线构建",
    }
    assert forbidden.isdisjoint(navigation.MAIN_NAVIGATION)


def test_app_registers_only_two_pages() -> None:
    source = _source("app.py")

    assert '"本体构建": ontology_builder.render' in source
    assert '"智能体 Demo": agent_demo.render' in source
    for old_name in ["demo_home", "sql_assets", "evaluation", "advanced", "legacy_app"]:
        assert old_name not in source


def test_navigation_uses_pending_state_before_widget() -> None:
    navigation_source = _source("navigation.py")
    state_source = _source("state.py")

    assert "apply_pending_navigation(MAIN_NAVIGATION)" in navigation_source
    assert "key=str(StateKey.CURRENT_PAGE)" in navigation_source
    assert "request_navigation" in state_source
    assert "PENDING_PAGE" in state_source


def test_no_active_page_writes_navigation_widget_key_directly() -> None:
    for page in ["pages/ontology_builder.py", "pages/agent_demo.py"]:
        source = _source(page)
        assert "set_value(StateKey.CURRENT_PAGE" not in source
        assert "set_value(StateKey.PAGE" not in source


def test_state_keys_cover_two_surface_flow() -> None:
    state = importlib.import_module("state")
    keys = {item.value for item in state.StateKey}

    for key in [
        "daa.page",
        "daa.pending_page",
        "daa.selected_data_source",
        "daa.selected_ontology_version",
        "daa.ontology.snapshot_id",
        "daa.ontology.construction_run_id",
        "daa.ontology.candidate_id",
        "daa.ontology.current_draft_id",
        "daa.ontology.draft_revision",
        "daa.chat.messages",
        "daa.query.result",
        "daa.ai.status",
    ]:
        assert key in keys


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
    assert counts == {
        "total": 6,
        "accepted": 1,
        "modified": 1,
        "rejected": 1,
        "merged": 1,
        "deferred": 1,
        "pending": 1,
    }
    assert state.completion_ratio(candidates) == pytest.approx(4 / 5)


def test_all_candidate_resource_forms_have_required_structured_fields() -> None:
    forms = importlib.import_module("components.resource_forms")
    required = {
        "object_type": {"id", "name", "plural_name", "description", "lifecycle_status"},
        "property": {"id", "object_type_id", "name", "data_type", "semantic_role", "unit"},
        "metric": {"id", "aggregation", "measure_property_id", "filter_predicates"},
        "dimension": {"id", "name", "property_id", "time_grain"},
        "link_type": {"id", "name", "source_object_type_id", "target_object_type_id"},
        "binding": {"id", "object_type_id", "data_source_id", "property_bindings"},
        "physical_join": {"id", "left_table", "left_column", "right_table", "right_column"},
    }

    assert set(forms.RESOURCE_FORM_FIELDS) == set(required)
    for resource_type, fields in required.items():
        assert fields <= set(forms.RESOURCE_FORM_FIELDS[resource_type])


def test_stable_id_is_read_only() -> None:
    source = _source("components/resource_forms.py")

    assert 'st.text_input("ID"' in source
    assert "disabled=True" in source


def test_modify_diff_detects_field_changes() -> None:
    diff_view = importlib.import_module("components.diff_view")

    before = {"id": "transaction_amount", "name": "交易金额"}
    after = {"id": "transaction_amount", "name": "已入账交易金额"}
    assert diff_view.changed_fields(before, after) == ["name"]
    assert "已入账交易金额" in diff_view.diff_text(before, after)


def test_reject_merge_defer_review_rules_are_present() -> None:
    source = _source("pages/ontology_workbench.py")

    assert "REJECT" in source and 'if decision == "REJECT" and not comment' in source
    assert "MERGE" in source and 'if decision == "MERGE" and not merge_target' in source
    assert "DEFER" in source
    assert "MODIFY 必须至少修改一个字段" in source
    reject_block = source[
        source.index('if decision == "REJECT" and not comment') :
        source.index('if cols[3].button("提交审核"')
    ]
    assert "submit_disabled = True" in reject_block


def test_json_editor_is_developer_mode_only() -> None:
    source = _source("pages/ontology_workbench.py")

    assert "高级 JSON 编辑" in source
    assert "DEVELOPER_MODE" in source
    assert "if _developer_mode()" in source


def test_ontology_builder_uses_api_data_sources_and_demo_data_status() -> None:
    source = _source("pages/ontology_builder.py")

    assert "/api/v1/demo/data-sources" in source
    assert "/api/v1/demo/data-status" in source
    assert "/api/v1/demo/data-initialize" in source
    assert "MiniBank PostgreSQL" not in source


def test_snapshot_creation_is_available() -> None:
    source = _source("pages/ontology_builder.py")

    assert "/api/v1/ontology/metadata-snapshots/raw" in source
    assert "扫描数据结构并生成快照" in source


def test_standard_and_live_construction_modes_map_to_o_c_and_o_d() -> None:
    source = _source("pages/ontology_builder.py")

    assert "evidence_mode = \"O-D\" if mode.startswith(\"O-D\") else \"O-C\"" in source
    assert '"catalog_mode": "GOVERNED_CATALOG"' in source
    assert '"construction_mode": "STRICT_CONSTRUCTION"' in source


def test_live_ai_unavailable_is_blocked_not_mocked() -> None:
    source = _source("pages/ontology_builder.py")

    assert "真实 AI 未配置，无法运行 O-D Live" in source
    assert "not ai_is_live_ready(ai_status)" in source
    assert "configured-live" not in source


def test_promote_to_draft_and_draft_restore_are_present() -> None:
    source = _source("pages/ontology_workbench.py")

    assert "promote-to-draft" in source
    assert "promoted_draft_id" in source
    assert "remember_draft" in source


def test_draft_crud_and_if_match_are_supported() -> None:
    source = _source("pages/ontology_workbench.py")
    api_source = _source("api_client.py")

    for method in [".post(", ".put(", ".delete("]:
        assert method in source
    assert 'headers["If-Match"]' in api_source


def test_revision_conflict_is_reported() -> None:
    api_source = _source("api_client.py")

    assert "DRAFT_REVISION_CONFLICT" in api_source
    assert "Draft 已被其他操作更新" in api_source


def test_validate_submit_approve_publish_activate_flow_exists() -> None:
    source = _source("pages/ontology_workbench.py")

    for token in ["validate", "submit", "approve", "publish", "activate"]:
        assert f"/{token}" in source or token.title() in source
    assert "request_navigation(\"智能体 Demo\")" in source


def test_agent_demo_uses_database_ontology_and_ai_selectors() -> None:
    source = _source("pages/agent_demo.py")

    assert "render_data_source_selector" in source
    assert "render_ontology_selector" in source
    assert "render_ai_status" in source
    assert "启用所选本体" in source


def test_agent_demo_fixed_ontology_mode_and_sql_asset_enabled() -> None:
    source = _source("pages/agent_demo.py")

    assert '"query_mode": "ontology"' in source
    assert '"sql_asset_enabled": True' in source
    assert "schema" not in source
    assert "rag" not in source


def test_agent_demo_chat_ui_and_examples() -> None:
    source = _source("pages/agent_demo.py")

    assert "st.chat_message" in source
    assert "st.chat_input" in source
    for question in ["查询交易金额", "按客户类型统计交易笔数", "查询活跃客户数"]:
        assert question in source


def test_query_result_business_explanation_and_folded_technical_detail() -> None:
    source = _source("components/query_result.py")

    assert "为什么这样查询" in source
    assert "查看 SQL" in source
    assert "技术详情" in source
    assert "你查询的是" in source


def test_ontology_selector_only_lists_ready_published_versions() -> None:
    source = _source("components/ontology_selector.py")

    assert 'item.get("status", "PUBLISHED") == "PUBLISHED"' in source
    assert 'item.get("artifact_status") == "READY"' in source
    assert "data_source_id in set" in source


def test_ai_status_does_not_expose_keys() -> None:
    source = _source("components/ai_status.py")

    assert "configured" in source
    assert "API_KEY" not in source


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
        request = httpx.Request(method, url)
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


def test_backend_demo_endpoints_are_declared() -> None:
    source = Path("apps/api/main.py").read_text(encoding="utf-8")

    for route in [
        "/api/v1/demo/data-sources",
        "/api/v1/demo/data-status",
        "/api/v1/demo/data-initialize",
        "/api/v1/demo/ontologies",
        "/api/v1/demo/ai-status",
        "/api/v1/demo/runtime-status",
    ]:
        assert route in source


def test_demo_data_initialize_is_allowlisted() -> None:
    source = Path("apps/api/main.py").read_text(encoding="utf-8")

    assert "data/ddl/001_schema.sql" in source
    assert "data/seed/002_seed.sql" in source
    initialize_source = source[
        source.index("def demo_data_initialize") :
        source.index('@app.get("/api/v1/demo/ontologies")')
    ]
    assert "source_path" not in initialize_source


def test_settings_include_live_ai_demo_controls() -> None:
    source = Path("src/data_asset_agents/core/config.py").read_text(encoding="utf-8")

    assert "demo_require_live_ai" in source
    assert "developer_mode" in source


def test_env_example_and_compose_expose_live_ai_without_keys() -> None:
    env = Path(".env.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for token in ["LLM_PROVIDER=deepseek", "EMBEDDING_PROVIDER=aliyun", "DEMO_REQUIRE_LIVE_AI"]:
        assert token in env
    assert "LLM_API_KEY=" in env
    assert "EMBEDDING_API_KEY=" in env
    assert "DEMO_REQUIRE_LIVE_AI" in compose


def test_hidden_legacy_backend_capabilities_remain_importable() -> None:
    legacy = importlib.import_module("legacy_app")

    assert hasattr(legacy, "legacy_page")
    assert hasattr(legacy, "sql_asset_page")
    assert hasattr(legacy, "evaluation_page")
    assert hasattr(legacy, "ontology_manager_page")


def test_removed_old_pages_are_not_runtime_imports() -> None:
    app_source = _source("app.py")
    for page in ["demo_home", "advanced", "sql_assets", "evaluation", "intelligent_query"]:
        assert page not in app_source


def test_streamlit_health_acceptance_checks_two_navigation_entries() -> None:
    script = Path("scripts/demo_flow_acceptance.ps1").read_text(encoding="utf-8")

    assert "function Get-Utf8Text" in script
    assert 'Get-Utf8Text "5pys5L2T5p6E5bu6"' in script
    assert 'Get-Utf8Text "5pm66IO95L2TIERlbW8="' in script
    assert "Streamlit app still exposes old product navigation entries" in script
