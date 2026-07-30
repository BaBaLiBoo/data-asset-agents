from __future__ import annotations

from typing import Any

import streamlit as st


def evidence_summary(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "类型": item.get("source_type") or item.get("source"),
            "表": item.get("table_name"),
            "字段": item.get("column_name"),
            "置信度": item.get("confidence"),
            "LLM": item.get("llm_generated", False),
            "证据": item.get("extracted_fact") or item.get("detail"),
        }
        for item in evidence
    ]


def render_evidence(evidence: list[dict[str, Any]]) -> None:
    if not evidence:
        st.info("暂无结构化证据。")
        return
    st.dataframe(evidence_summary(evidence), use_container_width=True, hide_index=True)
    with st.expander("查看证据 JSON"):
        st.json(evidence)
