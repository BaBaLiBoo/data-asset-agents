from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
EXAMPLES = [
    "查询近30天各分行信用卡交易金额和交易笔数。",
    "查询最近30天各机构消费金额和流水笔数。",
]

st.set_page_config(page_title="Data Asset Agents", page_icon="🧭", layout="wide")


def api_request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 120,
) -> Any:
    try:
        response = httpx.request(
            method,
            f"{API_BASE_URL}{path}",
            json=json,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except ValueError:
            detail = {"detail": str(exc)}
        raise RuntimeError(detail.get("detail", str(exc))) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"无法连接 API：{exc}") from exc


def text_to_sql_page() -> None:
    st.title("Data Asset Agents")
    st.caption("基于已发布本体语义层与 LangGraph 的 MiniBank Text-to-SQL 演示")
    with st.sidebar:
        st.header("查询设置")
        example = st.selectbox("示例问题", EXAMPLES)
        mode = st.radio("检索模式", ["ontology", "rag", "schema"], horizontal=True)
        st.info("ontology 为正式链路；rag/schema 当前复用同一语义链路。")
    question = st.text_area("自然语言问题", value=example, height=90)
    if not st.button("执行 Text-to-SQL", type="primary", use_container_width=True):
        return
    try:
        data = api_request(
            "POST",
            "/api/v1/query",
            json={"question": question, "query_mode": mode},
            timeout=60,
        )
    except RuntimeError as exc:
        st.error(str(exc))
        return

    st.subheader("执行概览")
    cols = st.columns(3)
    cols[0].metric("置信度", f"{data['confidence']:.0%}")
    cols[1].metric("执行步骤", len(data["trace_steps"]))
    execution = data.get("execution_result") or {}
    cols[2].metric("结果行数", execution.get("row_count", 0))
    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Semantic Query")
        st.json(data.get("semantic_query"))
        st.markdown("#### 匹配概念")
        st.dataframe(data.get("matched_concepts", []), use_container_width=True)
        st.markdown("#### 资产选择")
        st.write("候选表：", data.get("candidate_tables", []))
        st.write("最终表：", data.get("selected_tables", []))
        st.dataframe(data.get("rejected_tables", []), use_container_width=True)
    with right:
        st.markdown("#### LangGraph Trace")
        st.dataframe(data.get("trace_steps", []), use_container_width=True)
        st.markdown("#### Join Plan")
        st.json(data.get("join_plan"))
    st.subheader("生成 SQL")
    st.code(data.get("generated_sql", ""), language="sql")
    st.subheader("校验报告")
    st.json(data.get("validation_report"))
    st.subheader("查询结果")
    st.dataframe(execution.get("rows", []), use_container_width=True, hide_index=True)
    st.subheader("自然语言解释")
    st.success(data.get("explanation", ""))


def _metadata_rows(build: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in build["snapshot"]["tables"]:
        profiles = {item["column_name"]: item for item in table["profiles"]}
        for column in table["columns"]:
            profile = profiles[column["name"]]
            rows.append(
                {
                    "table": table["table_name"],
                    "column": column["name"],
                    "type": column["data_type"],
                    "comment": column.get("comment"),
                    "primary_key": column["name"] in table["primary_key"],
                    "null_rate": profile["null_rate"],
                    "unique_rate": profile["unique_rate"],
                    "minimum": profile.get("minimum"),
                    "maximum": profile.get("maximum"),
                    "top_values": profile["top_values"],
                    "masked_samples": profile["sample_values"],
                }
            )
    return rows


def ontology_builder_page() -> None:
    st.title("本体语义层离线构建与人工审核")
    st.caption("物理知识抽取 → 候选语义 → 人工审核 → 版本化发布；候选不会进入在线查询")
    with st.sidebar:
        st.header("构建设置")
        schema_name = st.text_input("Schema", "public")
        st.info("仅允许审核 YAML 中声明的物理表，样例值会限量并脱敏。")
        if st.button("开始离线构建", type="primary", use_container_width=True):
            try:
                with st.spinner("正在抽取元数据、解析历史 SQL 并生成候选……"):
                    st.session_state["ontology_build"] = api_request(
                        "POST",
                        "/api/v1/ontology/build",
                        json={
                            "schema_name": schema_name,
                            "sample_limit": 5,
                            "top_value_limit": 5,
                        },
                        timeout=180,
                    )
            except RuntimeError as exc:
                st.error(str(exc))

    build = st.session_state.get("ontology_build")
    metadata_tab, sql_tab, review_tab, publish_tab = st.tabs(
        ["物理元数据", "历史 SQL 证据", "候选审核", "版本发布"]
    )
    with metadata_tab:
        if not build:
            st.info("点击“开始离线构建”后展示表、字段、约束、统计和脱敏样例。")
        else:
            snapshot = build["snapshot"]
            st.write(
                f"快照：`{snapshot['id']}`，表数量：{len(snapshot['tables'])}，"
                f"捕获时间：{snapshot['captured_at']}"
            )
            st.dataframe(_metadata_rows(build), use_container_width=True, hide_index=True)
            with st.expander("主键、外键和索引详情"):
                st.json(
                    [
                        {
                            "table": table["table_name"],
                            "primary_key": table["primary_key"],
                            "foreign_keys": table["foreign_keys"],
                            "indexes": table["indexes"],
                        }
                        for table in snapshot["tables"]
                    ]
                )
    with sql_tab:
        if not build:
            st.info("构建后展示 SQLGlot 结构化解析和使用统计。")
        else:
            st.json(build["historical_summary"])
            for analysis in build["historical_sql"]:
                with st.expander(analysis.get("question") or analysis["id"]):
                    st.code(analysis["sql_text"], language="sql")
                    st.json(analysis)
    with review_tab:
        candidate_type = st.selectbox("候选类型", ["concept", "mapping", "join"])
        status_filter = st.selectbox(
            "审核状态", ["CANDIDATE", "VERIFIED", "REJECTED"]
        )
        try:
            candidates = api_request(
                "GET",
                "/api/v1/ontology/candidates",
                params={
                    "candidate_type": candidate_type,
                    "status_filter": status_filter,
                    "limit": 500,
                },
            )
        except RuntimeError as exc:
            candidates = []
            st.warning(str(exc))
        if not candidates:
            st.info("当前筛选条件下没有候选。")
        else:
            selected_id = st.selectbox(
                "选择候选",
                [item["id"] for item in candidates],
                format_func=lambda candidate_id: next(
                    (
                        item["payload"].get("business_name")
                        or item["payload"].get("concept_id")
                        or (
                            f"{item['payload'].get('left_table')} → "
                            f"{item['payload'].get('right_table')}"
                        )
                        for item in candidates
                        if item["id"] == candidate_id
                    ),
                    candidate_id,
                ),
            )
            selected = next(item for item in candidates if item["id"] == selected_id)
            st.json(selected)
            reviewer = st.text_input("审核人", "reviewer")
            note = st.text_area("审核说明")
            edits: dict[str, Any] = {}
            if candidate_type == "concept":
                payload = selected["payload"]
                edits["business_name"] = st.text_input(
                    "业务名称", payload["business_name"]
                )
                edits["semantic_property"] = st.text_input(
                    "语义属性", payload["semantic_property"]
                )
                synonyms = st.text_input("同义词（逗号分隔）", ",".join(payload["synonyms"]))
                edits["synonyms"] = [item.strip() for item in synonyms.split(",") if item.strip()]
            verify_col, reject_col = st.columns(2)
            if verify_col.button("通过并保存编辑", use_container_width=True):
                try:
                    api_request(
                        "POST",
                        f"/api/v1/ontology/candidates/{selected_id}/verify",
                        json={"reviewer": reviewer, "note": note, "edits": edits},
                    )
                    st.success("候选已审核通过。")
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
            if reject_col.button("拒绝候选", use_container_width=True):
                try:
                    api_request(
                        "POST",
                        f"/api/v1/ontology/candidates/{selected_id}/reject",
                        json={"reviewer": reviewer, "note": note, "edits": {}},
                    )
                    st.success("候选已拒绝。")
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
    with publish_tab:
        try:
            versions = api_request("GET", "/api/v1/ontology/versions")
        except RuntimeError as exc:
            versions = []
            st.warning(str(exc))
        st.dataframe(versions, use_container_width=True, hide_index=True)
        version_name = st.text_input("新版本号", "0.2.0")
        description = st.text_area("版本说明", "人工审核后的语义层版本")
        published_by = st.text_input("发布人", "reviewer")
        if st.button("发布正式本体版本", type="primary"):
            try:
                snapshot_id = build["snapshot"]["id"] if build else None
                result = api_request(
                    "POST",
                    "/api/v1/ontology/publish",
                    json={
                        "version": version_name,
                        "description": description,
                        "published_by": published_by,
                        "snapshot_id": snapshot_id,
                    },
                )
                st.success(f"本体版本 {result['version']} 已发布并切换为在线版本。")
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))


page = st.sidebar.radio("工作台", ["Text-to-SQL", "本体构建与审核"])
if page == "Text-to-SQL":
    text_to_sql_page()
else:
    ontology_builder_page()
