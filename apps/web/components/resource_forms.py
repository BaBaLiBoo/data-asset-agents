from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

RESOURCE_LABELS = {
    "object_type": "ObjectType",
    "property": "Property",
    "metric": "Metric",
    "dimension": "Dimension",
    "binding": "Binding",
    "link_type": "LinkType",
    "physical_join": "PhysicalJoin",
}

RESOURCE_FORM_FIELDS = {
    "object_type": [
        "id",
        "name",
        "plural_name",
        "description",
        "category",
        "primary_key_property_id",
        "title_property_id",
        "lifecycle_status",
        "synonyms",
    ],
    "property": [
        "id",
        "object_type_id",
        "name",
        "description",
        "data_type",
        "semantic_role",
        "nullable",
        "sensitive",
        "groupable",
        "filterable",
        "unit",
        "synonyms",
    ],
    "metric": [
        "id",
        "name",
        "description",
        "aggregation",
        "measure_property_id",
        "filter_predicates",
        "time_property_id",
        "supported_dimension_ids",
        "synonyms",
        "lifecycle_status",
    ],
    "dimension": [
        "id",
        "name",
        "description",
        "property_id",
        "time_grain",
        "synonyms",
        "lifecycle_status",
    ],
    "link_type": [
        "id",
        "name",
        "source_object_type_id",
        "target_object_type_id",
        "source_role_name",
        "target_role_name",
        "cardinality",
        "physical_join_ids",
        "lifecycle_status",
    ],
    "physical_join": [
        "id",
        "name",
        "left_table",
        "left_column",
        "right_table",
        "right_column",
        "relationship",
        "lifecycle_status",
        "evidence",
    ],
    "binding": [
        "id",
        "object_type_id",
        "data_source_id",
        "schema_name",
        "table_name",
        "primary_key_column",
        "property_bindings",
        "lifecycle_status",
        "sync_status",
    ],
}


def _csv(value: list[str] | None) -> str:
    return ", ".join(value or [])


def _list_from_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _options(values: list[str], current: str | None = None) -> list[str]:
    output = list(dict.fromkeys([item for item in [current, *values] if item]))
    return output or [current or ""]


def render_resource_form(
    resource_type: str,
    resource: dict[str, Any],
    *,
    object_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    dimension_ids: list[str] | None = None,
    join_ids: list[str] | None = None,
    table_columns: dict[str, list[str]] | None = None,
    disabled: bool = False,
    key_prefix: str = "resource",
) -> dict[str, Any]:
    object_ids = object_ids or []
    property_ids = property_ids or []
    dimension_ids = dimension_ids or []
    join_ids = join_ids or []
    table_columns = table_columns or {}
    edited = dict(resource)
    st.text_input("ID", value=str(resource.get("id", "")), disabled=True, key=f"{key_prefix}.id")

    if resource_type == "object_type":
        edited["name"] = st.text_input("名称", edited.get("name", ""), disabled=disabled)
        edited["plural_name"] = st.text_input(
            "复数名称", edited.get("plural_name", ""), disabled=disabled
        )
        edited["description"] = st.text_area(
            "业务边界描述", edited.get("description", ""), disabled=disabled
        )
        edited["category"] = st.selectbox(
            "对象分类",
            _options(["CANONICAL_OBJECT", "EVENT", "REFERENCE"], edited.get("category")),
            disabled=disabled,
        )
        edited["primary_key_property_id"] = st.selectbox(
            "主键属性",
            _options(property_ids, edited.get("primary_key_property_id")),
            disabled=disabled,
        )
        edited["title_property_id"] = (
            st.selectbox(
                "标题属性",
                _options(["", *property_ids], edited.get("title_property_id")),
                disabled=disabled,
            )
            or None
        )
        edited["lifecycle_status"] = st.selectbox(
            "生命周期",
            _options(["DRAFT", "ACTIVE", "DEPRECATED"], edited.get("lifecycle_status", "DRAFT")),
            disabled=disabled,
        )
        edited["synonyms"] = _list_from_csv(
            st.text_input("同义词", _csv(edited.get("synonyms")), disabled=disabled)
        )
    elif resource_type == "property":
        edited["object_type_id"] = st.selectbox(
            "所属对象", _options(object_ids, edited.get("object_type_id")), disabled=disabled
        )
        edited["name"] = st.text_input("名称", edited.get("name", ""), disabled=disabled)
        edited["description"] = st.text_area(
            "描述", edited.get("description", ""), disabled=disabled
        )
        edited["data_type"] = st.selectbox(
            "数据类型",
            _options(
                ["STRING", "INTEGER", "DECIMAL", "BOOLEAN", "DATE", "DATETIME"],
                edited.get("data_type"),
            ),
            disabled=disabled,
        )
        edited["semantic_role"] = st.selectbox(
            "Semantic Role",
            _options(
                ["IDENTIFIER", "ATTRIBUTE", "STATUS", "MEASURE", "DIMENSION", "TIME"],
                edited.get("semantic_role"),
            ),
            disabled=disabled,
        )
        cols = st.columns(5)
        edited["nullable"] = cols[0].checkbox(
            "可空", bool(edited.get("nullable", True)), disabled=disabled
        )
        edited["sensitive"] = cols[1].checkbox(
            "敏感", bool(edited.get("sensitive", False)), disabled=disabled
        )
        edited["groupable"] = cols[2].checkbox(
            "可分组", bool(edited.get("groupable", False)), disabled=disabled
        )
        edited["filterable"] = cols[3].checkbox(
            "可过滤", bool(edited.get("filterable", True)), disabled=disabled
        )
        edited["unit"] = (
            cols[4].text_input("单位", edited.get("unit") or "", disabled=disabled) or None
        )
        edited["synonyms"] = _list_from_csv(
            st.text_input("同义词", _csv(edited.get("synonyms")), disabled=disabled)
        )
    elif resource_type == "metric":
        edited["name"] = st.text_input("名称", edited.get("name", ""), disabled=disabled)
        edited["description"] = st.text_area(
            "描述", edited.get("description", ""), disabled=disabled
        )
        edited["aggregation"] = st.selectbox(
            "聚合方式",
            _options(
                ["SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX"], edited.get("aggregation")
            ),
            disabled=disabled,
        )
        edited["measure_property_id"] = st.selectbox(
            "Measure Property",
            _options(property_ids, edited.get("measure_property_id")),
            disabled=disabled,
        )
        filters = edited.get("filter_predicates") or []
        filter_rows = pd.DataFrame(
            [
                {
                    "property_id": item.get("property_id", ""),
                    "operator": item.get("operator", "EQ"),
                    "value": item.get("value", ""),
                }
                for item in filters
            ]
            or [{"property_id": "", "operator": "EQ", "value": ""}]
        )
        filter_rows = st.data_editor(
            filter_rows,
            num_rows="dynamic",
            disabled=disabled,
            use_container_width=True,
            key=f"{key_prefix}.metric_filters",
        )
        edited["filter_predicates"] = [
            row
            for row in filter_rows.to_dict("records")
            if row.get("property_id") and row.get("value") not in {None, ""}
        ]
        edited["time_property_id"] = (
            st.selectbox(
                "Time Property",
                _options(["", *property_ids], edited.get("time_property_id")),
                disabled=disabled,
            )
            or None
        )
        edited["supported_dimension_ids"] = st.multiselect(
            "支持维度",
            dimension_ids,
            default=[
                item for item in edited.get("supported_dimension_ids", []) if item in dimension_ids
            ],
            disabled=disabled,
        )
        edited["synonyms"] = _list_from_csv(
            st.text_input("同义词", _csv(edited.get("synonyms")), disabled=disabled)
        )
        edited["lifecycle_status"] = st.selectbox(
            "生命周期",
            _options(["DRAFT", "ACTIVE", "DEPRECATED"], edited.get("lifecycle_status", "ACTIVE")),
            disabled=disabled,
        )
    elif resource_type == "dimension":
        edited["name"] = st.text_input("名称", edited.get("name", ""), disabled=disabled)
        edited["description"] = st.text_area(
            "描述", edited.get("description", ""), disabled=disabled
        )
        edited["property_id"] = st.selectbox(
            "Property", _options(property_ids, edited.get("property_id")), disabled=disabled
        )
        edited["time_grain"] = (
            st.selectbox(
                "Time Grain",
                _options(["", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"], edited.get("time_grain")),
                disabled=disabled,
            )
            or None
        )
        edited["synonyms"] = _list_from_csv(
            st.text_input("同义词", _csv(edited.get("synonyms")), disabled=disabled)
        )
        edited["lifecycle_status"] = st.selectbox(
            "生命周期",
            _options(["DRAFT", "ACTIVE", "DEPRECATED"], edited.get("lifecycle_status", "ACTIVE")),
            disabled=disabled,
        )
    elif resource_type == "link_type":
        st.info("Business Link 描述业务关系；Physical Join 描述物理字段连接。")
        edited["name"] = st.text_input("业务关系名称", edited.get("name", ""), disabled=disabled)
        edited["source_object_type_id"] = st.selectbox(
            "Source Object",
            _options(object_ids, edited.get("source_object_type_id")),
            disabled=disabled,
        )
        edited["target_object_type_id"] = st.selectbox(
            "Target Object",
            _options(object_ids, edited.get("target_object_type_id")),
            disabled=disabled,
        )
        edited["source_role_name"] = st.text_input(
            "Source Role", edited.get("source_role_name", ""), disabled=disabled
        )
        edited["target_role_name"] = st.text_input(
            "Target Role", edited.get("target_role_name", ""), disabled=disabled
        )
        edited["cardinality"] = st.selectbox(
            "Cardinality",
            _options(
                ["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"],
                edited.get("cardinality"),
            ),
            disabled=disabled,
        )
        edited["physical_join_ids"] = st.multiselect(
            "关联 Physical Join",
            join_ids,
            default=[item for item in edited.get("physical_join_ids", []) if item in join_ids],
            disabled=disabled,
        )
        edited["lifecycle_status"] = st.selectbox(
            "生命周期",
            _options(["DRAFT", "ACTIVE", "DEPRECATED"], edited.get("lifecycle_status", "ACTIVE")),
            disabled=disabled,
        )
    elif resource_type == "physical_join":
        tables = sorted(table_columns)
        edited["name"] = st.text_input("名称", edited.get("name", ""), disabled=disabled)
        edited["left_table"] = st.selectbox(
            "左表", _options(tables, edited.get("left_table")), disabled=disabled
        )
        edited["left_column"] = st.selectbox(
            "左字段",
            _options(table_columns.get(edited["left_table"], []), edited.get("left_column")),
            disabled=disabled,
        )
        edited["right_table"] = st.selectbox(
            "右表", _options(tables, edited.get("right_table")), disabled=disabled
        )
        edited["right_column"] = st.selectbox(
            "右字段",
            _options(table_columns.get(edited["right_table"], []), edited.get("right_column")),
            disabled=disabled,
        )
        edited["relationship"] = st.selectbox(
            "Relationship",
            _options(
                ["many_to_one", "one_to_one", "one_to_many", "many_to_many"],
                edited.get("relationship"),
            ),
            disabled=disabled,
        )
        edited["lifecycle_status"] = st.selectbox(
            "生命周期",
            _options(["DRAFT", "ACTIVE", "DEPRECATED"], edited.get("lifecycle_status", "ACTIVE")),
            disabled=disabled,
        )
        edited["evidence"] = _list_from_csv(
            st.text_input("审核证据", _csv(edited.get("evidence")), disabled=disabled)
        )
    elif resource_type == "binding":
        tables = sorted(table_columns)
        edited["object_type_id"] = st.selectbox(
            "Object", _options(object_ids, edited.get("object_type_id")), disabled=disabled
        )
        edited["data_source_id"] = st.text_input(
            "Data Source", edited.get("data_source_id", "minibank-postgres"), disabled=disabled
        )
        edited["schema_name"] = st.text_input(
            "Schema", edited.get("schema_name", "public"), disabled=disabled
        )
        edited["table_name"] = st.selectbox(
            "Table", _options(tables, edited.get("table_name")), disabled=disabled
        )
        edited["primary_key_column"] = st.selectbox(
            "Primary Key Column",
            _options(table_columns.get(edited["table_name"], []), edited.get("primary_key_column")),
            disabled=disabled,
        )
        rows = pd.DataFrame(
            [
                {"property_id": key, "column": value}
                for key, value in (edited.get("property_bindings") or {}).items()
            ]
            or [{"property_id": "", "column": ""}]
        )
        rows = st.data_editor(
            rows,
            num_rows="dynamic",
            disabled=disabled,
            use_container_width=True,
            key=f"{key_prefix}.property_bindings",
        )
        edited["property_bindings"] = {
            row["property_id"]: row["column"]
            for row in rows.to_dict("records")
            if row.get("property_id") and row.get("column")
        }
        edited["schema_hash"] = edited.get("schema_hash") or "pending-validation"
        edited["sync_status"] = st.selectbox(
            "Sync Status",
            _options(
                ["UNCHECKED", "HEALTHY", "STALE", "DRIFTED", "FAILED"],
                edited.get("sync_status", "UNCHECKED"),
            ),
            disabled=disabled,
        )
    return edited
