from __future__ import annotations

import streamlit as st
from legacy_app import legacy_page


def render() -> None:
    st.title("高级管理")
    st.caption("保留旧的工程调试入口、原始 JSON、低层状态和兼容工具。")
    legacy_page()
