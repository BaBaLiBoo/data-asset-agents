from __future__ import annotations

import streamlit as st
from navigation import render_navigation
from pages import agent_demo, ontology_builder

st.set_page_config(page_title="Data Asset Agent Demo", page_icon="DA", layout="wide")


def main() -> None:
    render_navigation(
        {
            "本体构建": ontology_builder.render,
            "智能体 Demo": agent_demo.render,
        }
    )


if __name__ == "__main__":
    main()
