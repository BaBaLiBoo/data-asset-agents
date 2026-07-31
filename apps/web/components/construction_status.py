from __future__ import annotations

from typing import Any

import streamlit as st


def render_construction_status(run: dict[str, Any]) -> None:
    counts = run.get("candidate_counts") or {}
    cols = st.columns(6)
    cols[0].metric("Construction Run ID", run.get("run_id", "-"))
    cols[1].metric("状态", run.get("status", "-"))
    cols[2].metric("构建模式", run.get("evidence_mode", "-"))
    cols[3].metric("AI 模式", run.get("llm_mode", "-"))
    cols[4].metric("候选数量", sum(counts.values()) if isinstance(counts, dict) else "-")
    cols[5].metric("Warning", len(run.get("warnings") or []))
    st.dataframe(
        [
            {"资源类型": key, "数量": value}
            for key, value in sorted(counts.items())
        ],
        use_container_width=True,
        hide_index=True,
    )
