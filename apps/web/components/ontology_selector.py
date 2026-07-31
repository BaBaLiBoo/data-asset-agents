from __future__ import annotations

from typing import Any

import streamlit as st
from state import StateKey, get_value, set_value

PRODUCT_NAMES = {
    "quality-v2-gold-independent": "MiniBank 正式业务本体 v1.0",
    "reviewed-o-c": "自动候选审核版本",
    "reviewed-o-d": "AI 增强候选审核版本",
}


def display_name(version: dict[str, Any]) -> str:
    name = version.get("display_name") or PRODUCT_NAMES.get(version.get("version_name", ""))
    return name or version.get("version") or version.get("version_name") or version.get("id", "-")


def render_ontology_selector(
    versions: list[dict[str, Any]], data_source_id: str | None
) -> dict[str, Any] | None:
    ready = [
        item
        for item in versions
        if item.get("status", "PUBLISHED") == "PUBLISHED"
        and item.get("artifact_status") == "READY"
        and (not data_source_id or data_source_id in set(item.get("data_source_ids") or []))
    ]
    if not ready:
        st.warning("没有与当前数据源匹配且可激活的已发布本体。")
        return None
    labels = {}
    for item in ready:
        count = item.get("resource_count", item.get("resource_counts", {}).get("total", "-"))
        labels[f"{display_name(item)} · {count} 个资源"] = item
    current = get_value(StateKey.SELECTED_ONTOLOGY_VERSION)
    label_list = list(labels)
    index = 0
    for idx, label in enumerate(label_list):
        item = labels[label]
        if current in {item.get("version"), item.get("version_name"), item.get("id")}:
            index = idx
            break
    selected_label = st.selectbox("选择正式本体", label_list, index=index)
    selected = labels[selected_label]
    set_value(
        StateKey.SELECTED_ONTOLOGY_VERSION,
        selected.get("version") or selected.get("version_name"),
    )
    return selected
