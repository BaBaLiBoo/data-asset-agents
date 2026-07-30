from __future__ import annotations

from typing import Any

import streamlit as st


def render_version_badge(status: dict[str, Any] | None) -> None:
    if not status:
        st.warning("当前本体状态不可用。")
        return
    current = status.get("current_version") or {}
    artifact = status.get("current_artifact") or status.get("artifact") or {}
    display_name = status.get("display_name") or current.get("version") or "未发布"
    columns = st.columns(5)
    columns[0].metric("当前正式本体", display_name)
    columns[1].metric("Version ID", current.get("id") or "无")
    columns[2].metric("Bundle Hash", (artifact.get("bundle_hash") or "")[:12] or "无")
    columns[3].metric("Artifact", artifact.get("status") or "未知")
    columns[4].metric("SQLAsset", "READY" if status.get("sql_asset_build") else "未就绪")
