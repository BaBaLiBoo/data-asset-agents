from __future__ import annotations

from typing import Any

import streamlit as st


def render_data_status(status: dict[str, Any]) -> None:
    cols = st.columns(4)
    cols[0].metric("数据状态", "已准备" if status.get("ready") else "未准备")
    cols[1].metric("表数量", status.get("table_count", 0))
    cols[2].metric("记录数量", status.get("total_rows", 0))
    cols[3].metric("数据快照 Hash", (status.get("data_snapshot_hash") or "")[:12] or "-")
    tables = status.get("tables") or []
    if tables:
        st.dataframe(tables, use_container_width=True, hide_index=True)
    if status.get("ready"):
        st.success("MiniBank 演示数据已初始化。")
    else:
        st.info("只会导入仓库内受控的 MiniBank 虚构 DDL 和 Seed。")
