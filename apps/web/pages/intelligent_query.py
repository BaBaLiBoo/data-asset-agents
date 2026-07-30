from __future__ import annotations

from typing import Any

import streamlit as st
from api_client import get_client, handle_api_error
from components.version_badge import render_version_badge
from state import StateKey, get_value, set_value

EXAMPLE_QUESTIONS = [
    "查询交易金额",
    "按客户类型统计交易笔数",
    "查询近30天各分行信用卡交易金额",
    "查询各分行交易金额和排名",
    "查询各渠道信用卡交易金额",
    "查询活跃客户数",
]


def _semantic_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    semantic = data.get("semantic_query") or {}
    return [
        {"环节": "识别的业务指标", "内容": ", ".join(semantic.get("metric_ids") or [])},
        {"环节": "识别的维度", "内容": ", ".join(semantic.get("dimension_ids") or [])},
        {
            "环节": "时间和过滤条件",
            "内容": str(semantic.get("filters") or semantic.get("time_range") or ""),
        },
        {"环节": "匹配本体概念", "内容": str(data.get("matched_concepts") or [])},
        {
            "环节": "业务口径解释",
            "内容": data.get("business_summary") or data.get("explanation") or "",
        },
        {
            "环节": "物理表字段映射",
            "内容": str(data.get("selected_columns") or data.get("candidate_tables") or []),
        },
        {"环节": "Join Plan", "内容": str(data.get("join_plan") or {})},
        {"环节": "SQLAsset 是否命中", "内容": "是" if data.get("selected_sql_asset") else "否"},
    ]


def _why_section(data: dict[str, Any]) -> None:
    join_plan = data.get("join_plan") or {}
    st.markdown("### 为什么这样查询")
    rows = _semantic_rows(data)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    selected_asset = data.get("selected_sql_asset")
    if selected_asset:
        st.info(f"命中认证 SQLAsset：{selected_asset.get('id') or selected_asset}")
    if join_plan:
        st.json(join_plan)


def render() -> None:
    client = get_client()
    st.title("智能问数")
    try:
        status = client.get("/api/v1/ontology/demo-status", timeout=30)
        set_value(StateKey.DEMO_STATUS, status)
        render_version_badge(status)
    except Exception as exc:
        handle_api_error(exc)
        return

    with st.sidebar:
        st.header("查询设置")
        example = st.selectbox("演示问题", EXAMPLE_QUESTIONS)
        mode = st.radio("检索模式", ["ontology", "rag", "schema"], horizontal=True)
        sql_asset_enabled = st.toggle("启用认证 SQLAsset", value=True, disabled=mode != "ontology")
    question = st.text_area("自然语言问题", value=example, height=90)
    if st.button("执行智能问数", type="primary", use_container_width=True):
        try:
            data = client.post(
                "/api/v1/query",
                json={
                    "question": question,
                    "query_mode": mode,
                    "sql_asset_enabled": sql_asset_enabled,
                },
                timeout=90,
            )
            set_value(StateKey.QUERY_RESULT, data)
        except Exception as exc:
            handle_api_error(exc)
            return

    data = get_value(StateKey.QUERY_RESULT)
    if not data:
        st.info("选择一个演示问题后执行，结果会按业务理解、物理映射、SQL 和结果解释展开。")
        return

    execution = data.get("execution_result") or {}
    st.markdown("### 查询流程")
    st.dataframe(
        [{"环节": "用户问题", "内容": question}, *_semantic_rows(data)],
        use_container_width=True,
        hide_index=True,
    )
    _why_section(data)
    st.markdown("### 生成 SQL")
    st.code(data.get("generated_sql", ""), language="sql")
    st.markdown("### 校验结果")
    st.json(data.get("validation_report") or data.get("ontology_policy_report") or {})
    st.markdown("### 查询结果")
    st.dataframe(execution.get("rows", []), use_container_width=True, hide_index=True)
    st.markdown("### 自然语言解释")
    st.success(data.get("explanation", ""))
    with st.expander("查看技术细节"):
        st.json(data)
