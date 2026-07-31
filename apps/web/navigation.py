from __future__ import annotations

import os
from collections.abc import Callable

import streamlit as st
from state import StateKey, apply_pending_navigation

MAIN_NAVIGATION = ["本体构建", "智能体 Demo"]


def demo_mode_enabled() -> bool:
    return os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def developer_mode_enabled() -> bool:
    return os.getenv("DEVELOPER_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


def render_navigation(pages: dict[str, Callable[[], None]]) -> str:
    default_page = apply_pending_navigation(MAIN_NAVIGATION)
    default_index = MAIN_NAVIGATION.index(default_page)
    with st.sidebar:
        st.markdown("### Data Asset Agent")
        page = st.radio(
            "主导航",
            MAIN_NAVIGATION,
            index=default_index,
            key=str(StateKey.CURRENT_PAGE),
        )
        st.caption("Demo Mode" if demo_mode_enabled() else "Developer Mode")
    pages[page]()
    return page
