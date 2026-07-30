from __future__ import annotations

import os
from collections.abc import Callable

import streamlit as st
from state import StateKey

MAIN_NAVIGATION = [
    "演示首页",
    "本体构建与治理",
    "智能问数",
    "SQL 资产",
    "实验与评测",
    "高级管理",
]


def demo_mode_enabled() -> bool:
    return os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def render_navigation(pages: dict[str, Callable[[], None]]) -> str:
    default_page = st.session_state.get(
        str(StateKey.PAGE), "演示首页" if demo_mode_enabled() else "高级管理"
    )
    default_index = MAIN_NAVIGATION.index(default_page) if default_page in MAIN_NAVIGATION else 0
    with st.sidebar:
        st.markdown("### Data Asset Agent")
        page = st.radio("主导航", MAIN_NAVIGATION, index=default_index, key=str(StateKey.PAGE))
        st.caption("Demo Mode" if demo_mode_enabled() else "Developer Mode")
    pages[page]()
    return page
