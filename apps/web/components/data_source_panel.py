from __future__ import annotations

from typing import Any

import streamlit as st
from state import StateKey, get_value, set_value


def render_data_source_selector(data_sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not data_sources:
        st.warning("尚未注册可用数据源。")
        return None
    labels = {}
    for item in data_sources:
        if item.get("enabled", True):
            schema = item.get("schema_name") or item.get("schema")
            name = item.get("display_name") or item.get("name")
            provider = item.get("provider")
            label = f"{name} · {provider} · {schema}"
            labels[label] = item
    if not labels:
        st.warning("当前没有启用的数据源。")
        return None
    current_id = get_value(StateKey.SELECTED_DATA_SOURCE)
    label_list = list(labels)
    index = 0
    for idx, label in enumerate(label_list):
        if labels[label].get("id") == current_id:
            index = idx
            break
    selected_label = st.selectbox("选择数据源", label_list, index=index)
    selected = labels[selected_label]
    set_value(StateKey.SELECTED_DATA_SOURCE, selected["id"])
    cols = st.columns(6)
    cols[0].metric("Data Source ID", selected.get("id", "-"))
    cols[1].metric("Provider", selected.get("provider", "-"))
    cols[2].metric("Host", selected.get("host") or "-")
    cols[3].metric("Port", selected.get("port") or "-")
    cols[4].metric("Database", selected.get("database") or "-")
    cols[5].metric("Schema", selected.get("schema_name") or selected.get("schema") or "-")
    return selected
