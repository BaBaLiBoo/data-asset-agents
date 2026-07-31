from __future__ import annotations

import os
from typing import Any

import streamlit as st
from api_client import get_client, handle_api_error, success
from components.ai_status import render_ai_status
from components.data_source_panel import render_data_source_selector
from components.ontology_selector import display_name, render_ontology_selector
from components.query_result import render_query_result
from state import (
    StateKey,
    get_value,
    request_navigation,
    set_value,
)

EXAMPLE_QUESTIONS = [
    "查询交易金额",
    "按客户类型统计交易笔数",
    "查询近30天各分行信用卡交易金额",
    "查询各分行交易金额和排名",
    "查询各渠道信用卡交易金额",
    "查询活跃客户数",
]


def _developer_mode() -> bool:
    return os.getenv("DEVELOPER_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_chat_state() -> list[dict[str, Any]]:
    messages = get_value(StateKey.CHAT_MESSAGES)
    if not isinstance(messages, list):
        messages = []
        set_value(StateKey.CHAT_MESSAGES, messages)
    return messages


def _load_options() -> dict[str, Any]:
    return get_client().get("/api/v1/demo/runtime-status", timeout=30)


def _selected_version_key(version: dict[str, Any] | None) -> str | None:
    if not version:
        return None
    return version.get("version") or version.get("version_name")


def _activate_selected(version: dict[str, Any]) -> None:
    version_name = _selected_version_key(version)
    if not version_name:
        st.error("所选本体缺少 Version Name，无法激活。")
        return
    try:
        get_client().post(f"/api/v1/ontology/versions/{version_name}/activate", timeout=180)
        success("运行配置已就绪，在线本体和查询图已刷新。")
        st.rerun()
    except Exception as exc:
        handle_api_error(exc)


def _run_question(question: str) -> None:
    messages = _ensure_chat_state()
    messages.append({"role": "user", "content": question})
    try:
        data = get_client().post(
            "/api/v1/query",
            json={"question": question, "query_mode": "ontology", "sql_asset_enabled": True},
            timeout=120,
        )
        set_value(StateKey.LAST_QUERY_RESULT, data)
        messages.append(
            {
                "role": "assistant",
                "content": data.get("explanation") or data.get("status") or "查询完成",
                "result": data,
            }
        )
    except Exception as exc:
        handle_api_error(exc)
        messages.append({"role": "assistant", "content": f"查询失败：{exc}", "result": None})
    set_value(StateKey.CHAT_MESSAGES, messages)


def _render_runtime_header(
    source: dict[str, Any] | None, version: dict[str, Any] | None, ai_status: dict[str, Any]
) -> None:
    chat = ai_status.get("chat") or {}
    st.info(
        "数据库："
        f"{(source or {}).get('display_name') or (source or {}).get('name') or '-'} ｜ "
        f"本体：{display_name(version or {}) if version else '-'} ｜ "
        f"AI：{chat.get('provider') or '-'} / {chat.get('model') or '-'} / "
        f"{'Live' if ai_status.get('mode') == 'live' else 'Mock'}"
    )


def render() -> None:
    st.title("智能体 Demo")
    st.caption("选择数据库、正式本体和 AI 模型，通过自然语言完成数据问答。")
    try:
        runtime = _load_options()
    except Exception as exc:
        handle_api_error(exc)
        return

    data_sources = runtime.get("data_sources") or []
    versions = runtime.get("ontologies") or []
    ai_status = runtime.get("ai_status") or {}

    st.subheader("查询环境")
    source = render_data_source_selector(data_sources)
    version = render_ontology_selector(versions, (source or {}).get("id"))
    render_ai_status(ai_status)
    _render_runtime_header(source, version, ai_status)
    st.caption("本 Demo 为单用户演示环境，切换正式本体会更新当前在线运行版本。")

    current = runtime.get("current_version") or {}
    selected_key = _selected_version_key(version)
    if version and selected_key != current.get("version"):
        confirm = st.checkbox("确认启用所选本体")
        if st.button("启用所选本体", type="primary", disabled=not confirm):
            _activate_selected(version)
    else:
        st.success("所选本体已经是当前在线运行版本。")

    st.subheader("对话区")
    cols = st.columns([1, 1])
    if cols[0].button("返回本体构建", use_container_width=True):
        request_navigation("本体构建")
        st.rerun()
    if cols[1].button("清空当前对话", use_container_width=True):
        set_value(StateKey.CHAT_MESSAGES, [])
        set_value(StateKey.LAST_QUERY_RESULT, None)
        st.rerun()

    st.markdown("演示问题")
    buttons = st.columns(3)
    for idx, question in enumerate(EXAMPLE_QUESTIONS):
        if buttons[idx % 3].button(question, key=f"example.{idx}", use_container_width=True):
            _run_question(question)
            st.rerun()

    for message in _ensure_chat_state():
        with st.chat_message(message["role"]):
            st.write(message["content"])

    question = st.chat_input("输入自然语言问题")
    if question:
        _run_question(question)
        st.rerun()

    result = get_value(StateKey.LAST_QUERY_RESULT)
    st.subheader("查询解释")
    if result:
        render_query_result(result)
    else:
        st.info("请选择演示问题或输入问题。每条问题独立执行，当前聊天历史仅用于页面展示。")

    if _developer_mode():
        with st.expander("开发者诊断信息"):
            st.json(
                {
                    "runtime": runtime,
                    "query_mode": "ontology",
                    "sql_asset_enabled": True,
                    "selected_data_source": get_value(StateKey.SELECTED_DATA_SOURCE),
                    "selected_ontology_version": get_value(StateKey.SELECTED_ONTOLOGY_VERSION),
                }
            )
