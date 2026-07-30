from __future__ import annotations

import os
from typing import Any

import streamlit as st
from api_client import get_client, handle_api_error, success
from components.status import render_check_table, render_resource_counts
from components.version_badge import render_version_badge
from state import StateKey, set_value


def _version_cards(status: dict[str, Any]) -> None:
    versions = [
        ("自动候选审核版本", "Reviewed O-C", "元数据、画像和认证历史 SQL 生成后审核。"),
        ("LLM 增强候选审核版本", "Reviewed O-D", "在 O-C 证据基础上增加大模型语义建议。"),
        ("MiniBank 正式业务本体 v1.0", "Official", "完整人工审核、发布并可用于正式演示。"),
    ]
    columns = st.columns(3)
    for column, (title, subtitle, body) in zip(columns, versions, strict=False):
        with column:
            st.markdown(f"#### {title}")
            st.caption(subtitle)
            st.write(body)


def render() -> None:
    client = get_client()
    st.title("Data Asset Agent Demo")
    st.caption("自动构建、人工治理、智能问数的一体化本体语义平台")
    try:
        status = client.get("/api/v1/ontology/demo-status", timeout=30)
        set_value(StateKey.DEMO_STATUS, status)
    except Exception as exc:
        handle_api_error(exc)
        st.info("API 恢复后，本页会显示 PostgreSQL、Ontology Artifact、Index 和 SQLAsset 状态。")
        return

    render_version_badge(status)
    st.markdown("### 当前运行状态")
    render_check_table(status.get("health_checks", []))

    st.markdown("### 当前本体信息")
    current = status.get("current_version") or {}
    configured = status.get("configured_demo_version") or {}
    artifact = status.get("current_artifact") or status.get("artifact") or {}
    info_cols = st.columns(6)
    info_cols[0].metric("产品展示名称", status.get("display_name") or "未配置")
    info_cols[1].metric("内部 Version ID", current.get("id") or "无")
    info_cols[2].metric("Bundle Hash", (artifact.get("bundle_hash") or "")[:12] or "无")
    info_cols[3].metric("当前激活", "是" if current.get("is_current") else "否")
    info_cols[4].metric("发布人", current.get("published_by") or "无")
    info_cols[5].metric("发布时间", current.get("published_at") or "无")
    render_resource_counts(status.get("resource_counts") or {})

    action_cols = st.columns(3)
    if action_cols[0].button("进入本体构建与治理", type="primary", use_container_width=True):
        set_value(StateKey.PAGE, "本体构建与治理")
        st.rerun()
    if action_cols[1].button("使用正式本体智能问数", use_container_width=True):
        set_value(StateKey.PAGE, "智能问数")
        st.rerun()

    demo_version_name = os.getenv("DEMO_ONTOLOGY_VERSION_NAME", "quality-v2-gold-independent")
    is_demo_active = current.get("version") == demo_version_name
    if configured and not is_demo_active:
        if action_cols[2].button("启用正式演示本体", use_container_width=True):
            try:
                client.post(
                    f"/api/v1/ontology/versions/{configured['version']}/activate", timeout=180
                )
                status = client.get("/api/v1/ontology/demo-status", timeout=30)
                set_value(StateKey.DEMO_STATUS, status)
                success("正式演示本体已激活，OntologyService 已重新加载。")
                st.rerun()
            except Exception as exc:
                handle_api_error(exc)
    elif configured:
        action_cols[2].success("正式演示本体已激活")
    else:
        action_cols[2].warning(f"未找到 {demo_version_name}")

    st.markdown("### 六步流程")
    st.write("数据源扫描 -> 候选生成 -> 人工审核 -> 草稿完善 -> 发布激活 -> 智能问数")

    st.markdown("### 演示版本")
    _version_cards(status)

    st.markdown("### Demo 一键检查")
    if st.button("检查演示环境", use_container_width=True):
        try:
            checked = client.get(
                "/api/v1/ontology/demo-status",
                params={"run_example_checks": True},
                timeout=180,
            )
            set_value(StateKey.DEMO_STATUS, checked)
            render_check_table(checked.get("health_checks", []))
            st.markdown("#### 示例查询检查")
            render_check_table(checked.get("example_query_checks", []))
        except Exception as exc:
            handle_api_error(exc)
