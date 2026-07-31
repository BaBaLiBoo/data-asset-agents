from __future__ import annotations

from typing import Any

import streamlit as st


def render_ai_status(status: dict[str, Any]) -> None:
    chat = status.get("chat") or {}
    embedding = status.get("embedding") or {}
    live_ready = bool(status.get("live_ready"))
    cols = st.columns(4)
    cols[0].metric("AI 模式", "Live" if status.get("mode") == "live" else "Mock")
    cols[1].metric("Provider", chat.get("provider") or "-")
    cols[2].metric("Model", chat.get("model") or "-")
    cols[3].metric("可用", "可用" if live_ready or status.get("mode") == "mock" else "不可用")
    st.caption(
        "Embedding: "
        f"{embedding.get('provider') or '-'} / {embedding.get('model') or '-'} / "
        f"configured={bool(embedding.get('configured'))}"
    )
    if status.get("mode") == "live" and not live_ready:
        st.error(status.get("blocking_reason") or "真实 AI 未配置，无法运行 Live 模式。")


def ai_is_live_ready(status: dict[str, Any]) -> bool:
    return status.get("mode") == "live" and bool(status.get("live_ready"))
