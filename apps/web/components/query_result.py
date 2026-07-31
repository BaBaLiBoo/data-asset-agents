from __future__ import annotations

from typing import Any

import streamlit as st


def _ids(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def why_text(data: dict[str, Any]) -> str:
    semantic = data.get("semantic_query") or {}
    metric = _ids(data.get("metrics") or semantic.get("metric_ids"))
    dimensions = _ids(data.get("dimensions") or semantic.get("dimension_ids"))
    selected = data.get("selected_columns") or {}
    tables = _ids(data.get("selected_tables") or data.get("candidate_tables"))
    return (
        f"你查询的是“{metric}”。\n\n"
        "系统先用当前正式本体识别业务指标和分析维度，"
        f"本次维度为 {dimensions}。\n\n"
        f"物理执行会使用 {tables}，字段映射为 {selected or '-'}。\n\n"
        "如果命中认证 SQLAsset，会优先复用已认证模板；否则使用本体中的绑定和 Join 规则生成 SQL。"
    )


def render_query_result(data: dict[str, Any]) -> None:
    execution = data.get("execution_result") or {}
    st.markdown("#### 业务回答")
    st.success(data.get("explanation") or data.get("status") or "查询完成")
    rows = execution.get("rows") or []
    st.markdown("#### 查询结果")
    st.caption(f"执行状态：{data.get('status')}，返回 {len(rows)} 行")
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.markdown("#### 为什么这样查询")
    st.write(why_text(data))
    semantic = data.get("semantic_query") or {}
    st.dataframe(
        [
            {
                "环节": "业务指标",
                "内容": _ids(data.get("metrics") or semantic.get("metric_ids")),
            },
            {
                "环节": "分析维度",
                "内容": _ids(data.get("dimensions") or semantic.get("dimension_ids")),
            },
            {"环节": "时间范围", "内容": _ids(semantic.get("time_range"))},
            {"环节": "固定过滤条件", "内容": _ids(semantic.get("filters"))},
            {"环节": "本体概念", "内容": _ids(data.get("matched_concepts"))},
            {"环节": "物理表", "内容": _ids(data.get("selected_tables"))},
            {"环节": "物理字段", "内容": _ids(data.get("selected_columns"))},
            {"环节": "Join Plan", "内容": _ids(data.get("join_plan"))},
            {"环节": "SQLAsset 是否命中", "内容": "是" if data.get("selected_sql_asset") else "否"},
            {
                "环节": "校验状态",
                "内容": _ids(
                    data.get("validation_report") or data.get("ontology_policy_report")
                ),
            },
        ],
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("查看 SQL"):
        st.code(data.get("generated_sql") or "", language="sql")
    with st.expander("技术详情"):
        st.json(data)
