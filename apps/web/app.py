from __future__ import annotations

import streamlit as st
from navigation import render_navigation
from pages import advanced, demo_home, evaluation, intelligent_query, ontology_workbench, sql_assets

st.set_page_config(page_title="Data Asset Agent Demo", page_icon="DA", layout="wide")


def main() -> None:
    render_navigation(
        {
            "演示首页": demo_home.render,
            "本体构建与治理": ontology_workbench.render,
            "智能问数": intelligent_query.render,
            "SQL 资产": sql_assets.render,
            "实验与评测": evaluation.render,
            "高级管理": advanced.render,
        }
    )


if __name__ == "__main__":
    main()
