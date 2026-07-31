from __future__ import annotations

import streamlit as st

WORKFLOW_STEPS = ["选择数据源", "准备数据", "自动构建", "人工审核", "草稿完善", "发布本体"]


def render_stepper(
    completed: set[str], current: str, steps: list[str] | None = None
) -> None:
    active_steps = steps or WORKFLOW_STEPS
    columns = st.columns(len(active_steps))
    for index, step in enumerate(active_steps, start=1):
        if step == current:
            state = "进行中"
        elif step in completed:
            state = "完成"
        else:
            state = "待处理"
        columns[index - 1].metric(f"{index}. {step}", state)
