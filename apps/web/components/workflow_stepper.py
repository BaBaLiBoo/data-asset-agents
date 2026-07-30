from __future__ import annotations

import streamlit as st

WORKFLOW_STEPS = [
    "数据源扫描",
    "自动构建",
    "候选审核",
    "草稿完善",
    "校验发布",
    "完成",
]


def render_stepper(completed: set[str], current: str) -> None:
    columns = st.columns(len(WORKFLOW_STEPS))
    for index, step in enumerate(WORKFLOW_STEPS, start=1):
        if step == current:
            state = "进行中"
        elif step in completed:
            state = "完成"
        else:
            state = "待处理"
        columns[index - 1].metric(f"{index}. {step}", state)
