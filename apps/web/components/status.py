from __future__ import annotations

from typing import Any

import streamlit as st


def status_label(status: str | None) -> str:
    if not status:
        return "WARNING"
    upper = str(status).upper()
    if upper in {"READY", "PASSED", "VALID", "PUBLISHED", "ACTIVE", "OK", "UP"}:
        return "PASSED"
    if upper in {"FAILED", "BLOCKED", "ERROR", "DOWN"}:
        return "BLOCKED"
    return "WARNING"


def render_check_table(checks: list[dict[str, Any]]) -> None:
    rows = [
        {
            "检查项": item.get("name"),
            "状态": item.get("status"),
            "说明": item.get("detail"),
            "修复步骤": item.get("fix", ""),
        }
        for item in checks
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_resource_counts(counts: dict[str, int]) -> None:
    labels = [
        ("对象", "object_types"),
        ("属性", "properties"),
        ("指标", "metrics"),
        ("维度", "dimensions"),
        ("业务关系", "link_types"),
        ("绑定", "bindings"),
        ("Physical Join", "physical_joins"),
    ]
    columns = st.columns(len(labels))
    for column, (label, key) in zip(columns, labels, strict=False):
        column.metric(label, counts.get(key, 0))
