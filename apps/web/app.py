from __future__ import annotations

import difflib
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
        sql_asset_enabled = st.toggle(
            "启用认证 SQLAsset", value=True, disabled=mode != "ontology"
        )
        st.info("schema、rag 与 ontology 使用严格隔离的查询策略。")
    question = st.text_area("自然语言问题", value=example, height=90)
    if not st.button("执行 Text-to-SQL", type="primary", use_container_width=True):
        return
    try:
        data = api_request(
            "POST",
            "/api/v1/query",
            json={
                "question": question,
                "query_mode": mode,
                "sql_asset_enabled": sql_asset_enabled,
            },
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
    st.subheader("认证历史 SQL 资产")
    candidates = data.get("sql_asset_candidates", [])
    if candidates:
        score_rows = [
            {
                "id": item["asset"]["id"],
                "question": item["asset"]["question"],
                "certification": item["asset"]["certification_level"],
                "lifecycle_valid": item["asset"]["lifecycle_valid"],
                **item["score"],
            }
            for item in candidates
        ]
        st.dataframe(score_rows, use_container_width=True, hide_index=True)
        with st.expander("选中模板、检索证据与 AST 改写差异"):
            st.json(
                {
                    "selected": data.get("selected_sql_asset"),
                    "selected_template_rank": data.get("selected_template_rank"),
                    "template_rejection_reasons": data.get(
                        "template_rejection_reasons", {}
                    ),
                    "rewrite": data.get("sql_rewrite"),
                    "evidence": candidates[0].get("evidence", []),
                }
            )
    else:
        st.info("当前没有通过全部安全门槛的认证 SQL 模板，使用确定性编译器。")
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
                    "snapshot_id": (
                        build["snapshot"]["id"] if build is not None else None
                    ),
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
        snapshot_id = build["snapshot"]["id"] if build else None
        publish_payload = {
            "version": version_name,
            "description": description,
            "published_by": published_by,
            "snapshot_id": snapshot_id,
        }
        dry_col, publish_col = st.columns(2)
        if dry_col.button("发布前 Dry Run", use_container_width=True):
            try:
                result = api_request(
                    "POST",
                    "/api/v1/ontology/publish/validate",
                    json=publish_payload,
                )
                st.session_state["publish_dry_run"] = result
                if result["valid"]:
                    st.success("契约、SQLGlot 和 PostgreSQL EXPLAIN 均通过。")
                else:
                    st.error("Dry Run 未通过，正式发布已被禁止。")
                st.json(result)
            except RuntimeError as exc:
                st.error(str(exc))
        if publish_col.button(
            "发布正式本体版本", type="primary", use_container_width=True
        ):
            try:
                result = api_request(
                    "POST", "/api/v1/ontology/publish", json=publish_payload
                )
                st.success(f"本体版本 {result['version']} 已发布并切换为在线版本。")
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
        if versions:
            inactive_versions = [item["version"] for item in versions if not item["is_current"]]
            if inactive_versions:
                rollback_version = st.selectbox("切换或回滚到已发布版本", inactive_versions)
                if st.button("激活所选版本", use_container_width=True):
                    try:
                        result = api_request(
                            "POST",
                            f"/api/v1/ontology/versions/{rollback_version}/activate",
                        )
                        st.success(f"已激活版本 {result['version']}，在线 Graph 已重建。")
                        st.rerun()
                    except RuntimeError as exc:
                        st.error(str(exc))


def sql_asset_page() -> None:
    st.title("认证历史 SQL 资产")
    st.caption("SQLGlot 结构解析 → 生命周期与 Join 审核 → EXPLAIN → 混合检索")
    with st.sidebar:
        if st.button("从受控历史文件重建索引", type="primary", use_container_width=True):
            try:
                report = api_request("POST", "/api/v1/sql-assets/build", json={})
                st.success(
                    f"构建 {report['build']['build_id']} 已进入 "
                    f"{report['build']['status']}；索引 {report['indexed']} 条，"
                    f"其中 {report['eligible']} 条可召回。"
                )
                st.json(report["build"])
            except RuntimeError as exc:
                st.error(str(exc))
    question = st.text_input("检索问题", "查询近30天各分行信用卡交易金额")
    search_col, refresh_col = st.columns(2)
    results: list[dict[str, Any]] = []
    if search_col.button("混合检索", use_container_width=True):
        try:
            results = api_request(
                "POST",
                "/api/v1/sql-assets/search",
                json={"question": question, "limit": 10},
            )
        except RuntimeError as exc:
            st.error(str(exc))
    try:
        assets = api_request("GET", "/api/v1/sql-assets", params={"limit": 200})
    except RuntimeError as exc:
        assets = []
        st.warning(str(exc))
    if refresh_col.button("刷新资产清单", use_container_width=True):
        st.rerun()
    if results:
        st.subheader("检索与重排结果")
        st.dataframe(
            [
                {
                    "id": item["asset"]["id"],
                    "question": item["asset"]["question"],
                    "certification": item["asset"]["certification_level"],
                    **item["score"],
                }
                for item in results
            ],
            use_container_width=True,
            hide_index=True,
        )
        selected_result = st.selectbox(
            "查看候选详情",
            results,
            format_func=lambda item: (
                f"{item['asset']['question']} · {item['score']['total']:.3f}"
            ),
        )
        st.write("检索证据：", selected_result["evidence"])
        st.code(selected_result["asset"]["sql_text"], language="sql")
        st.json(selected_result)
    st.subheader("全部资产（包含被安全门槛排除的记录）")
    st.dataframe(
        [
            {
                "id": item["id"],
                "question": item["question"],
                "certified": item["certified"],
                "certification_level": item["certification_level"],
                "execution_status": item["execution_status"],
                "ontology_version_id": item["ontology_version_id"],
                "build_id": item["build_id"],
                "lifecycle_valid": item["lifecycle_valid"],
                "semantic_policy_valid": item["semantic_policy_valid"],
                "metric_policy_violations": item["metric_policy_violations"],
                "invalid_columns": item["invalid_columns"],
                "unapproved_joins": item["unapproved_joins"],
            }
            for item in assets
        ],
        use_container_width=True,
        hide_index=True,
    )


def evaluation_page() -> None:
    st.title("模式对比与评测")
    st.caption(
        "Schema / Physical RAG / Ontology without SQLAsset / Ontology full 严格隔离对照"
    )
    st.warning("SMOKE 结果仅用于工程验证，不能代表真实模型效果。")
    variants = {
        "Schema": ("schema", "schema", False),
        "Physical RAG": ("rag", "rag", False),
        "Ontology without SQLAsset": (
            "ontology",
            "ontology_no_sql_asset",
            False,
        ),
        "Ontology full": ("ontology", "ontology_full", True),
    }
    with st.sidebar:
        st.header("评测设置")
        max_cases = st.number_input("Smoke 案例数", 1, 20, 4)
        concurrency = st.number_input("并发数", 1, 8, 1)
        selected_variants = st.multiselect(
            "实验组", list(variants), default=list(variants)
        )
        if st.button("创建四组 Smoke 运行", type="primary", use_container_width=True):
            created: list[str] = []
            try:
                for label in selected_variants:
                    mode, variant, assets = variants[label]
                    run = api_request(
                        "POST",
                        "/api/v1/evaluation/runs",
                        json={
                            "query_mode": mode,
                            "strategy_variant": variant,
                            "sql_asset_enabled": assets,
                            "run_kind": "smoke",
                            "max_cases": int(max_cases),
                            "concurrency": int(concurrency),
                        },
                    )
                    created.append(run["run_id"])
                st.session_state["evaluation_run_ids"] = created
                st.success(f"已创建 {len(created)} 个后台评测运行。")
            except RuntimeError as exc:
                st.error(str(exc))
        if st.button("刷新运行状态", use_container_width=True):
            st.rerun()

    try:
        runs = api_request("GET", "/api/v1/evaluation/runs", params={"limit": 100})
    except RuntimeError as exc:
        st.error(str(exc))
        return
    st.subheader("运行记录")
    st.dataframe(runs, use_container_width=True, hide_index=True)
    completed = [run for run in runs if run["status"] == "COMPLETED"]
    if not completed:
        st.info("暂无已完成运行。后台运行完成后点击刷新。")
        return
    default_ids = st.session_state.get("evaluation_run_ids", [])
    completed_ids = {run["run_id"] for run in completed}
    compare_ids = st.multiselect(
        "选择对比运行",
        [run["run_id"] for run in completed],
        default=[item for item in default_ids if item in completed_ids],
    )
    allow_mismatch = st.checkbox("允许显示不满足公平条件的对比", False)
    if compare_ids:
        try:
            comparison = api_request(
                "GET",
                "/api/v1/evaluation/compare",
                params={"run_id": compare_ids, "allow_mismatch": allow_mismatch},
            )
            if comparison["warnings"]:
                st.warning("；".join(comparison["warnings"]))
            st.subheader("总体指标")
            st.dataframe(comparison["runs"], use_container_width=True, hide_index=True)
        except RuntimeError as exc:
            st.warning(str(exc))

    selected_run = st.selectbox(
        "案例详情运行",
        completed,
        format_func=lambda run: f"{run['strategy_variant']} · {run['run_id']}",
    )
    try:
        cases = api_request(
            "GET", f"/api/v1/evaluation/runs/{selected_run['run_id']}/cases"
        )
    except RuntimeError as exc:
        st.error(str(exc))
        return
    categories = sorted({item["category"] for item in cases if item.get("category")})
    difficulties = sorted(
        {item["difficulty"] for item in cases if item.get("difficulty")}
    )
    category = st.selectbox("类别过滤", ["全部", *categories])
    difficulty = st.selectbox("难度过滤", ["全部", *difficulties])
    failure_types = sorted(
        {item["failure_category"] for item in cases if item["failure_category"]}
    )
    failure_type = st.selectbox("失败类型过滤", ["全部", *failure_types])
    filtered = [
        item
        for item in cases
        if (category == "全部" or item.get("category") == category)
        and (difficulty == "全部" or item.get("difficulty") == difficulty)
        and (failure_type == "全部" or item["failure_category"] == failure_type)
    ]
    st.dataframe(filtered, use_container_width=True, hide_index=True)
    if filtered:
        selected_case = st.selectbox(
            "查看 SQL、检索上下文和失败证据",
            filtered,
            format_func=lambda item: f"{item['case_id']} · {item['predicted_status']}",
        )
        left, right = st.columns(2)
        with left:
            st.markdown("#### 问题与 Gold")
            st.write(selected_case.get("question") or "无问题文本")
            st.json(selected_case.get("gold") or {})
            st.markdown("#### 生成 SQL")
            st.code(selected_case.get("generated_sql") or "无 SQL", language="sql")
            st.markdown("#### 检索上下文 / Semantic Output")
            st.json(
                {
                    "retrieved_context": selected_case.get("retrieved_context"),
                    "semantic_output": selected_case.get("semantic_output"),
                }
            )
        with right:
            gold_sql = ((selected_case.get("gold") or {}).get("sql") or "").splitlines()
            generated_sql = (selected_case.get("generated_sql") or "").splitlines()
            sql_diff = "\n".join(
                difflib.unified_diff(
                    gold_sql,
                    generated_sql,
                    fromfile="gold.sql",
                    tofile="generated.sql",
                    lineterm="",
                )
            )
            st.markdown("#### SQL 差异")
            st.code(sql_diff or "SQL 一致或无可比较 SQL", language="diff")
            st.markdown("#### 校验、Hash 与失败原因")
            st.json(selected_case)
    export_col1, export_col2 = st.columns(2)
    export_col1.link_button(
        "导出 JSON",
        f"{API_BASE_URL}/api/v1/evaluation/runs/{selected_run['run_id']}/export?format=json",
        use_container_width=True,
    )
    export_col2.link_button(
        "导出 CSV",
        f"{API_BASE_URL}/api/v1/evaluation/runs/{selected_run['run_id']}/export?format=csv",
        use_container_width=True,
    )


page = st.sidebar.radio(
    "工作台",
    ["Text-to-SQL", "模式对比与评测", "认证 SQL 资产", "本体构建与审核"],
)
if page == "Text-to-SQL":
    text_to_sql_page()
elif page == "模式对比与评测":
    evaluation_page()
elif page == "认证 SQL 资产":
    sql_asset_page()
else:
    ontology_builder_page()
