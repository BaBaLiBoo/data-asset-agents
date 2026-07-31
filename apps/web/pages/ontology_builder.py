from __future__ import annotations

from typing import Any

import streamlit as st
from api_client import get_client, handle_api_error, success
from components.ai_status import ai_is_live_ready, render_ai_status
from components.candidate_review import render_candidate_review
from components.construction_status import render_construction_status
from components.data_import_panel import render_data_status
from components.data_source_panel import render_data_source_selector
from components.draft_editor import render_draft_editor
from components.publish_panel import render_publish_panel
from components.workflow_stepper import WORKFLOW_STEPS, render_stepper
from pages.ontology_workbench import _load_draft, _load_run_and_candidates, _version_management
from state import (
    FINAL_CANDIDATE_STATUSES,
    StateKey,
    get_value,
    set_value,
)


def _current_step(
    data_status: dict[str, Any] | None,
    run: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    draft: dict[str, Any] | None,
) -> tuple[str, set[str]]:
    completed: set[str] = set()
    if get_value(StateKey.SELECTED_DATA_SOURCE):
        completed.add("选择数据源")
    if data_status and data_status.get("ready") and get_value(StateKey.SNAPSHOT_ID):
        completed.add("准备数据")
    if run and run.get("status") not in {"CREATED", "FAILED"}:
        completed.add("自动构建")
    if candidates and all(item["status"] in FINAL_CANDIDATE_STATUSES for item in candidates):
        completed.add("人工审核")
    if draft:
        completed.add("草稿完善")
    if draft and draft.get("status") == "PUBLISHED":
        completed.add("发布本体")
    for step in WORKFLOW_STEPS:
        if step not in completed:
            return step, completed
    return "发布本体", completed


def _load_demo_status() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    client = get_client()
    data_sources = client.get("/api/v1/demo/data-sources", timeout=30)
    data_status = client.get("/api/v1/demo/data-status", timeout=30)
    ai_status = client.get("/api/v1/demo/ai-status", timeout=30)
    set_value(StateKey.AI_STATUS, ai_status)
    return data_sources, data_status, ai_status


def _data_source_step(data_sources: list[dict[str, Any]]) -> None:
    st.subheader("1. 选择数据源")
    selected = render_data_source_selector(data_sources)
    if not selected:
        return
    cols = st.columns(3)
    if cols[0].button("检查数据源", use_container_width=True):
        try:
            inspection = get_client().post(
                f"/api/v1/ontology/data-sources/{selected['id']}/inspect", timeout=60
            )
            st.session_state["daa.datasource.inspection"] = inspection
            success("数据源连接正常。")
        except Exception as exc:
            handle_api_error(exc)
    if cols[1].button("查看数据库概要", use_container_width=True):
        st.session_state["daa.show_data_status"] = True
    cols[2].button("进入下一步", use_container_width=True, disabled=True)
    inspection = st.session_state.get("daa.datasource.inspection")
    if inspection:
        st.dataframe([inspection], use_container_width=True, hide_index=True)


def _data_prepare_step(data_status: dict[str, Any]) -> None:
    st.subheader("2. 准备数据")
    render_data_status(data_status)
    cols = st.columns(2)
    confirm = cols[0].checkbox("确认仅导入仓库内 MiniBank 虚构演示数据")
    if cols[1].button(
        "导入 MiniBank 演示数据",
        type="primary",
        disabled=data_status.get("ready") or not confirm,
        use_container_width=True,
    ):
        try:
            initialized = get_client().post("/api/v1/demo/data-initialize", timeout=180)
            success(initialized.get("message", "MiniBank 演示数据已初始化。"))
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    if st.button("扫描数据结构并生成快照", type="primary", use_container_width=True):
        try:
            build = get_client().post(
                "/api/v1/ontology/metadata-snapshots/raw",
                json={"schema_name": "public", "sample_limit": 5, "top_value_limit": 5},
                timeout=180,
            )
            snapshot = build["snapshot"]
            set_value(StateKey.SNAPSHOT_ID, snapshot["id"])
            set_value(StateKey.SNAPSHOT_PAYLOAD, build)
            success("数据结构快照已生成。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    build = get_value(StateKey.SNAPSHOT_PAYLOAD)
    if build:
        snapshot = build["snapshot"]
        cols = st.columns(4)
        cols[0].metric("Snapshot", snapshot.get("id", "-"))
        cols[1].metric("表数量", len(snapshot.get("tables") or []))
        cols[2].metric("Snapshot Hash", (snapshot.get("snapshot_hash") or "")[:12])
        cols[3].metric("捕获时间", snapshot.get("captured_at", "-"))
        st.dataframe(
            [
                {
                    "表": table.get("table_name"),
                    "字段数": len(table.get("columns") or []),
                    "主键": ", ".join(table.get("primary_key") or []),
                    "外键": len(table.get("foreign_keys") or []),
                    "Profiling": len(table.get("profiles") or []),
                }
                for table in snapshot.get("tables") or []
            ],
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("查看技术详情"):
            st.json(build)


def _construction_step(run: dict[str, Any] | None, ai_status: dict[str, Any]) -> None:
    st.subheader("3. 自动构建")
    render_ai_status(ai_status)
    mode = st.radio(
        "构建方式",
        ["O-C：元数据、字段画像和认证历史 SQL", "O-D：O-C 加真实 AI 语义建议"],
        horizontal=True,
        index=0,
    )
    evidence_mode = "O-D" if mode.startswith("O-D") else "O-C"
    live_selected = evidence_mode == "O-D"
    if live_selected and not ai_is_live_ready(ai_status):
        st.error(
            "真实 AI 未配置，无法运行 O-D Live。"
            "请配置 DeepSeek 和 DashScope Key，或切换 O-C。"
        )
    snapshot_id = get_value(StateKey.SNAPSHOT_ID)
    if st.button(
        "创建自动构建 Run",
        type="primary",
        disabled=not snapshot_id or (live_selected and not ai_is_live_ready(ai_status)),
    ):
        try:
            created = get_client().post(
                "/api/v1/ontology/construction-runs",
                json={
                    "source_snapshot_id": snapshot_id,
                    "catalog_mode": "GOVERNED_CATALOG",
                    "construction_mode": "STRICT_CONSTRUCTION",
                    "evidence_mode": evidence_mode,
                    "llm_mode": "live" if live_selected else "mock",
                    "provider": ai_status.get("chat", {}).get("provider", "mock")
                    if live_selected
                    else "mock",
                    "model": ai_status.get("chat", {}).get("model", "deterministic-rules-v1")
                    if live_selected
                    else "deterministic-rules-v1",
                    "temperature": 0,
                    "random_seed": 20260715,
                    "created_by": "streamlit-reviewer",
                },
                timeout=60,
            )
            set_value(StateKey.CONSTRUCTION_RUN_ID, created["run_id"])
            success("自动构建 Run 已创建。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    if run:
        render_construction_status(run)
        if st.button("生成候选", type="primary", disabled=run.get("status") != "CREATED"):
            try:
                generated = get_client().post(
                    f"/api/v1/ontology/construction-runs/{run['run_id']}/generate",
                    timeout=180,
                )
                set_value(StateKey.CONSTRUCTION_RUN_ID, generated["run_id"])
                success("候选已生成，请进入人工审核。")
                st.rerun()
            except Exception as exc:
                handle_api_error(exc)
        with st.expander("开发者诊断信息"):
            st.json(run)


def _versions_step() -> None:
    st.subheader("已发布本体")
    _version_management()


def render() -> None:
    st.title("本体构建")
    st.caption("面向数据管理员或业务专家，完成从数据准备到正式本体发布的完整流程。")
    try:
        data_sources, data_status, ai_status = _load_demo_status()
    except Exception as exc:
        handle_api_error(exc)
        data_sources, data_status, ai_status = [], {}, {}
    run, candidates = _load_run_and_candidates()
    draft_detail = _load_draft()
    current_step, completed = _current_step(
        data_status, run, candidates, draft_detail["draft"] if draft_detail else None
    )
    render_stepper(completed, current_step)
    tabs = st.tabs(WORKFLOW_STEPS)
    with tabs[0]:
        _data_source_step(data_sources)
    with tabs[1]:
        _data_prepare_step(data_status)
    with tabs[2]:
        _construction_step(run, ai_status)
    with tabs[3]:
        if run and candidates:
            render_candidate_review(run, candidates)
        else:
            st.info("请先完成自动构建并生成候选。")
    with tabs[4]:
        if draft_detail:
            render_draft_editor(draft_detail)
        else:
            st.info("候选全部审核完成后，可以生成正式本体草稿。")
    with tabs[5]:
        if draft_detail:
            render_publish_panel(draft_detail)
        else:
            st.info("请先生成或打开 Draft。")
        _versions_step()
