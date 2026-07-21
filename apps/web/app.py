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
    headers: dict[str, str] = {}
    if (
        method.upper() in {"POST", "PUT", "DELETE"}
        and "/api/v1/ontology/drafts/" in path
        and "manager_revision" in st.session_state
    ):
        headers["If-Match"] = f'"{st.session_state["manager_revision"]}"'
    try:
        response = httpx.request(
            method,
            f"{API_BASE_URL}{path}",
            json=json,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except ValueError:
            detail = {"detail": str(exc)}
        if detail.get("error_code") == "DRAFT_REVISION_CONFLICT":
            st.session_state["manager_revision"] = detail.get("current_revision")
            raise RuntimeError(
                "Draft 已被其他操作更新，请刷新后重新编辑。"
            ) from exc
        message = detail.get("detail", str(exc))
        if isinstance(message, dict):
            message = message.get("detail", str(message))
        raise RuntimeError(message) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"无法连接 API：{exc}") from exc


def text_to_sql_page() -> None:
    st.title("Data Asset Agents")
    st.caption("基于已发布本体语义层与 LangGraph 的 MiniBank Text-to-SQL 演示")
    with st.sidebar:
        st.header("查询设置")
        example = st.selectbox("示例问题", EXAMPLES)
        mode = st.radio("检索模式", ["ontology", "rag", "schema"], horizontal=True)
        sql_asset_enabled = st.toggle("启用认证 SQLAsset", value=True, disabled=mode != "ontology")
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
                    "template_rejection_reasons": data.get("template_rejection_reasons", {}),
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
        status_filter = st.selectbox("审核状态", ["CANDIDATE", "VERIFIED", "REJECTED"])
        try:
            candidates = api_request(
                "GET",
                "/api/v1/ontology/candidates",
                params={
                    "candidate_type": candidate_type,
                    "status_filter": status_filter,
                    "snapshot_id": (build["snapshot"]["id"] if build is not None else None),
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
                edits["business_name"] = st.text_input("业务名称", payload["business_name"])
                edits["semantic_property"] = st.text_input("语义属性", payload["semantic_property"])
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
        if publish_col.button("发布正式本体版本", type="primary", use_container_width=True):
            try:
                result = api_request("POST", "/api/v1/ontology/publish", json=publish_payload)
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
            format_func=lambda item: f"{item['asset']['question']} · {item['score']['total']:.3f}",
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
    st.caption("Schema / Physical RAG / Ontology without SQLAsset / Ontology full 严格隔离对照")
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
        selected_variants = st.multiselect("实验组", list(variants), default=list(variants))
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
        cases = api_request("GET", f"/api/v1/evaluation/runs/{selected_run['run_id']}/cases")
    except RuntimeError as exc:
        st.error(str(exc))
        return
    categories = sorted({item["category"] for item in cases if item.get("category")})
    difficulties = sorted({item["difficulty"] for item in cases if item.get("difficulty")})
    category = st.selectbox("类别过滤", ["全部", *categories])
    difficulty = st.selectbox("难度过滤", ["全部", *difficulties])
    failure_types = sorted({item["failure_category"] for item in cases if item["failure_category"]})
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


def ontology_manager_page() -> None:
    """Object-first editor backed by isolated Draft APIs."""

    st.title("Ontology Manager")
    st.caption("业务对象、属性、Link 与受控物理绑定；未发布 Draft 不影响在线查询")
    try:
        drafts = api_request("GET", "/api/v1/ontology/drafts")
        graph = api_request("GET", "/api/v1/ontology/object-graph")
        sources = api_request("GET", "/api/v1/ontology/data-sources")
        metrics = api_request("GET", "/api/v1/ontology/metrics")
        table_assets = api_request("GET", "/api/v1/ontology/tables")
        drift_reports = api_request("GET", "/api/v1/ontology/drift")
        index_builds = api_request("GET", "/api/v1/ontology/index-builds")
    except RuntimeError as exc:
        st.error(str(exc))
        return

    with st.sidebar:
        with st.form("manager_create_draft"):
            name = st.text_input("Draft 名称", "MiniBank object model")
            author = st.text_input("创建人", "ontology-manager")
            snapshot_id = st.text_input(
                "Metadata Snapshot ID（候选生成需要）",
                placeholder="先在本体构建页执行一次元数据抽取",
            )
            if st.form_submit_button("创建空 Draft", use_container_width=True):
                created = api_request(
                    "POST",
                    "/api/v1/ontology/drafts",
                    json={
                        "name": name,
                        "created_by": author,
                        "source_snapshot_id": snapshot_id or None,
                    },
                )
                st.session_state["manager_draft"] = created["draft"]["id"]
                st.rerun()
        if st.button("从对象种子创建 Draft", type="primary", use_container_width=True):
            seeded = api_request(
                "POST",
                "/api/v1/ontology/drafts/from-seed",
                json={
                    "draft_name": name,
                    "created_by": author,
                    "seed_name": "retail_banking",
                    "source_snapshot_id": snapshot_id or None,
                },
            )
            st.session_state["manager_draft"] = seeded["draft"]["id"]
            st.rerun()
        published_version_id = graph.get("version_id")
        if published_version_id and st.button(
            "复制当前发布版本", use_container_width=True
        ):
            copied = api_request(
                "POST",
                "/api/v1/ontology/drafts",
                json={
                    "name": name,
                    "created_by": author,
                    "base_version_id": published_version_id,
                    "source_snapshot_id": snapshot_id or None,
                },
            )
            st.session_state["manager_draft"] = copied["draft"]["id"]
            st.rerun()
        with st.expander("兼容工具：迁移旧 YAML"):
            st.caption("仅用于旧版本迁移和回归，不是新 Draft 的默认入口。")
            if st.button("运行 Legacy YAML 迁移", use_container_width=True):
                migrated = api_request(
                    "POST",
                    "/api/v1/ontology/drafts/migrate-legacy",
                    json={
                        "draft_name": f"{name} legacy migration",
                        "created_by": author,
                        "source_snapshot_id": snapshot_id or None,
                    },
                )
                st.session_state["manager_draft"] = migrated["draft"]["id"]
                st.rerun()

    objects = api_request("GET", "/api/v1/ontology/object-types")
    links = api_request("GET", "/api/v1/ontology/link-types")
    columns = st.columns(8)
    columns[0].metric("正式版本", graph.get("version_id") or "YAML fallback")
    columns[1].metric("Draft", len(drafts))
    columns[2].metric("对象", len(objects))
    columns[3].metric("Link", len(links))
    columns[4].metric("指标", len(metrics))
    columns[5].metric("数据源", len(sources))
    columns[6].metric(
        "Schema Drift",
        sum(item["severity"] == "BREAKING" for item in drift_reports),
    )
    current_indexes = [item for item in index_builds if item["is_current"]]
    columns[7].metric("当前索引", len(current_indexes))
    if graph.get("nodes"):
        dot = ["digraph ontology {", "rankdir=LR;"]
        dot += [f'"{node["id"]}" [label="{node["label"]}"];' for node in graph["nodes"]]
        dot += [
            f'"{edge["source"]}" -> "{edge["target"]}" [label="{edge["label"]}"];'
            for edge in graph["edges"]
        ]
        st.graphviz_chart("\n".join([*dot, "}"]), use_container_width=True)
    else:
        st.info("尚未发布对象模型。请从对象种子创建 Draft，或从空白 Draft 开始建模。")

    if sources:
        source_col, inspect_col = st.columns([4, 1])
        source_col.dataframe(
            [
                {key: item[key] for key in ("id", "name", "provider", "connection_ref", "enabled")}
                for item in sources
            ],
            use_container_width=True,
            hide_index=True,
        )
        if inspect_col.button("检查数据源"):
            health = api_request(
                "POST", f"/api/v1/ontology/data-sources/{sources[0]['id']}/inspect"
            )
            inspect_col.success(
                f"{'健康' if health['healthy'] else '异常'} · {health['table_count']} tables"
            )
    if not drafts:
        return

    labels = {f"{item['name']} · {item['status']}": item["id"] for item in drafts}
    selected = st.selectbox("当前 Draft", list(labels))
    draft_id = labels[selected]
    detail = api_request("GET", f"/api/v1/ontology/drafts/{draft_id}")
    draft, resources = detail["draft"], detail["resources"]
    st.session_state["manager_revision"] = draft["resource_revision"]
    editable = draft["status"] == "DRAFT"
    validation_fresh = (
        draft["validation_state"] == "VALID"
        and draft.get("validated_revision") == draft["resource_revision"]
        and draft.get("validated_hash") == draft["resource_hash"]
    )
    current_artifact = None
    if graph.get("version_id"):
        try:
            current_artifact = api_request(
                "GET",
                f"/api/v1/ontology/versions/{graph['version_id']}/compiled-artifact",
            )
        except RuntimeError:
            current_artifact = None
    governance = st.columns(8)
    governance[0].metric("Draft 状态", draft["status"])
    governance[1].metric("Resource Revision", draft["resource_revision"])
    governance[2].metric("Resource Hash", draft["resource_hash"][:12])
    governance[3].metric("Validation", draft["validation_state"])
    governance[4].metric("Validated Revision", draft.get("validated_revision") or "—")
    governance[5].metric("Submitted Revision", draft.get("submitted_revision") or "—")
    governance[6].metric("未校验修改", "否" if validation_fresh else "是")
    governance[7].metric(
        "Current Artifact",
        current_artifact["status"] if current_artifact else "LEGACY",
    )
    if editable and not validation_fresh:
        st.warning("当前修改尚未重新校验。请先 Validate，之后才能 Submit。")
    if draft["status"] == "IN_REVIEW":
        st.info(f"审核快照：revision {draft['submitted_revision']} · {draft['submitted_hash']}")
    bound_property_ids = {
        property_id
        for binding in resources["bindings"]
        for property_id in binding["property_bindings"]
    }
    total_properties = len(resources["properties"])
    coverage = len(bound_property_ids) / total_properties if total_properties else 0.0
    primary_properties = {
        item["primary_key_property_id"]
        for item in resources["object_types"]
        if item.get("primary_key_property_id")
    }
    primary_binding_ok = bool(primary_properties) and primary_properties <= bound_property_ids
    coverage_columns = st.columns(4)
    coverage_columns[0].metric("属性", total_properties)
    coverage_columns[1].metric("已绑定属性", len(bound_property_ids))
    coverage_columns[2].metric("绑定覆盖率", f"{coverage:.0%}")
    coverage_columns[3].metric("主键绑定", "完整" if primary_binding_ok else "待补充")
    tabs = st.tabs(
        [
            "对象类型",
            "属性",
            "指标",
            "维度",
            "数据源映射",
            "业务关系",
            "Physical Join",
            "元数据候选",
            "校验与发布",
            "Draft Diff",
            "Drift / Index",
            "Object Explorer",
        ]
    )
    with tabs[0]:
        st.dataframe(
            [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "primary_key": item["primary_key_property_id"],
                    "title": item["title_property_id"],
                    "property_count": len(item["property_ids"]),
                    "status": item["lifecycle_status"],
                }
                for item in resources["object_types"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        with st.form("manager_object_form"):
            object_id = st.text_input("对象 ID", "customer")
            object_name = st.text_input("名称", "客户")
            description = st.text_area("对象边界描述", "虚构 MiniBank 业务对象")
            lifecycle = st.selectbox("生命周期", ["DRAFT", "ACTIVE", "DEPRECATED"])
            if st.form_submit_button("保存对象", disabled=not editable):
                api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/object-types",
                    json={
                        "id": object_id,
                        "name": object_name,
                        "plural_name": f"{object_name}集合",
                        "description": description,
                        "lifecycle_status": lifecycle,
                    },
                )
                st.rerun()
    with tabs[1]:
        st.dataframe(resources["properties"], use_container_width=True, hide_index=True)
        object_ids = [item["id"] for item in resources["object_types"]]
        if object_ids:
            with st.form("manager_property_form"):
                owner = st.selectbox("所属对象", object_ids)
                suffix = st.text_input("属性 ID", "new_property")
                property_name = st.text_input("属性名称", "新属性")
                data_type = st.selectbox(
                    "类型", ["STRING", "INTEGER", "DECIMAL", "BOOLEAN", "DATE", "DATETIME"]
                )
                role = st.selectbox(
                    "语义角色",
                    ["IDENTIFIER", "ATTRIBUTE", "STATUS", "MEASURE", "DIMENSION", "TIME"],
                )
                flag_cols = st.columns(3)
                filterable = flag_cols[0].checkbox("可筛选", True)
                groupable = flag_cols[1].checkbox("可分组")
                sensitive = flag_cols[2].checkbox("敏感")
                if st.form_submit_button("保存属性", disabled=not editable):
                    api_request(
                        "POST",
                        f"/api/v1/ontology/drafts/{draft_id}/properties",
                        json={
                            "id": f"{owner}.{suffix}",
                            "object_type_id": owner,
                            "name": property_name,
                            "data_type": data_type,
                            "semantic_role": role,
                            "filterable": filterable,
                            "groupable": groupable,
                            "sensitive": sensitive,
                        },
                    )
                    st.rerun()
    with tabs[2]:
        st.caption("Metric 只引用业务 Property/Dimension；物理表达式在发布时确定性编译。")
        st.dataframe(resources["metrics"], use_container_width=True, hide_index=True)
        property_ids = [item["id"] for item in resources["properties"]]
        dimension_ids = [item["id"] for item in resources["dimensions"]]
        if property_ids:
            with st.form("manager_metric_form"):
                metric_id = st.text_input("Metric ID", "new_metric")
                metric_name = st.text_input("指标名称", "新指标")
                metric_description = st.text_area("业务口径", "描述统计范围与计算含义")
                measure_property = st.selectbox("度量 Property", property_ids)
                aggregation = st.selectbox(
                    "聚合方式", ["SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX"]
                )
                filter_property = st.selectbox("固定过滤 Property", ["(none)", *property_ids])
                filter_value = st.text_input("固定过滤值", "")
                time_properties = [
                    item["id"]
                    for item in resources["properties"]
                    if item["semantic_role"] == "TIME"
                ]
                time_property = st.selectbox("时间 Property", ["(none)", *time_properties])
                supported_dimensions = st.multiselect("支持的 Dimension", dimension_ids)
                if st.form_submit_button("保存 Metric", disabled=not editable):
                    predicates = (
                        [
                            {
                                "property_id": filter_property,
                                "operator": "EQ",
                                "value": filter_value,
                            }
                        ]
                        if filter_property != "(none)" and filter_value
                        else []
                    )
                    api_request(
                        "POST",
                        f"/api/v1/ontology/drafts/{draft_id}/metrics",
                        json={
                            "id": metric_id,
                            "name": metric_name,
                            "description": metric_description,
                            "measure_property_id": measure_property,
                            "aggregation": aggregation,
                            "filter_predicates": predicates,
                            "time_property_id": (
                                None if time_property == "(none)" else time_property
                            ),
                            "supported_dimension_ids": supported_dimensions,
                            "lifecycle_status": "ACTIVE",
                        },
                    )
                    st.rerun()
    with tabs[3]:
        st.caption("Dimension 只选择可分组 Property；table/column 不再人工录入。")
        st.dataframe(resources["dimensions"], use_container_width=True, hide_index=True)
        groupable_properties = [
            item["id"] for item in resources["properties"] if item["groupable"]
        ]
        if groupable_properties:
            with st.form("manager_dimension_form"):
                dimension_id = st.text_input("Dimension ID", "new_dimension")
                dimension_name = st.text_input("维度名称", "新维度")
                dimension_description = st.text_area("维度含义", "描述可分组的业务口径")
                dimension_property = st.selectbox("业务 Property", groupable_properties)
                if st.form_submit_button("保存 Dimension", disabled=not editable):
                    api_request(
                        "POST",
                        f"/api/v1/ontology/drafts/{draft_id}/dimensions",
                        json={
                            "id": dimension_id,
                            "name": dimension_name,
                            "description": dimension_description,
                            "property_id": dimension_property,
                            "lifecycle_status": "ACTIVE",
                        },
                    )
                    st.rerun()
    with tabs[5]:
        joins = {item["id"]: item for item in resources["physical_joins"]}
        st.dataframe(
            [
                {
                    "business_link": item["id"],
                    "source": item["source_object_type_id"],
                    "target": item["target_object_type_id"],
                    "cardinality": item["cardinality"],
                    "physical_join": ", ".join(item["physical_join_ids"]),
                    "expression": ", ".join(
                        joins[join_id]["left_table"]
                        + "."
                        + joins[join_id]["left_column"]
                        + " = "
                        + joins[join_id]["right_table"]
                        + "."
                        + joins[join_id]["right_column"]
                        for join_id in item["physical_join_ids"]
                        if join_id in joins
                    ),
                }
                for item in resources["link_types"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption("业务 Link 表达对象关系；Physical Join 仅描述审核过的字段连接。")
        object_ids = [item["id"] for item in resources["object_types"]]
        join_ids = list(joins)
        if len(object_ids) > 1 and join_ids:
            with st.form("manager_link_form"):
                link_id = st.text_input("Link ID", "transaction_belongs_to_branch")
                link_name = st.text_input("业务关系名称", "交易归属分行")
                source = st.selectbox("源对象", object_ids)
                target = st.selectbox("目标对象", object_ids, index=1)
                cardinality = st.selectbox(
                    "基数",
                    ["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"],
                )
                physical_join = st.selectbox("审核 Physical Join", join_ids)
                if st.form_submit_button("保存业务 Link", disabled=not editable):
                    api_request(
                        "POST",
                        f"/api/v1/ontology/drafts/{draft_id}/link-types",
                        json={
                            "id": link_id,
                            "name": link_name,
                            "source_object_type_id": source,
                            "target_object_type_id": target,
                            "source_role_name": target,
                            "target_role_name": f"{source}s",
                            "cardinality": cardinality,
                            "physical_join_ids": [physical_join],
                        },
                    )
                    st.rerun()
    with tabs[4]:
        for binding in resources["bindings"]:
            st.markdown(
                f"**{binding['object_type_id']} → "
                f"{binding['schema_name']}.{binding['table_name']}**"
            )
            st.dataframe(
                [
                    {"property": prop, "column": column}
                    for prop, column in binding["property_bindings"].items()
                ],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"schema_hash {binding['schema_hash'][:16]}… · {binding['sync_status']}")
        active_tables = [
            item for item in table_assets if item["status"] == "ACTIVE" and item["selectable"]
        ]
        object_ids = [item["id"] for item in resources["object_types"]]
        if object_ids and active_tables:
            with st.form("manager_binding_form"):
                bound_object = st.selectbox("绑定对象", object_ids)
                selected_table = st.selectbox("物理表", [item["name"] for item in active_tables])
                asset = next(item for item in active_tables if item["name"] == selected_table)
                primary_key = st.selectbox("主键字段", asset["columns"])
                owned_properties = [
                    item["id"]
                    for item in resources["properties"]
                    if item["object_type_id"] == bound_object
                ]
                property_id = st.selectbox("属性", owned_properties or [f"{bound_object}.unmapped"])
                column_name = st.selectbox("字段", asset["columns"])
                if st.form_submit_button("保存单属性绑定", disabled=not editable):
                    api_request(
                        "POST",
                        f"/api/v1/ontology/drafts/{draft_id}/bindings",
                        json={
                            "id": f"{bound_object}_primary_binding",
                            "object_type_id": bound_object,
                            "data_source_id": "minibank-postgres",
                            "schema_name": "public",
                            "table_name": selected_table,
                            "primary_key_column": primary_key,
                            "property_bindings": {property_id: column_name},
                            "schema_hash": "pending-validation",
                        },
                    )
                    st.rerun()
    with tabs[7]:
        st.caption(
            "候选来自 Draft 固定的 Metadata Snapshot、字段画像与历史 SQL 证据；"
            "生成和导入都不会自动发布。"
        )
        candidate_key = f"object_candidates_{draft_id}"
        if st.button("生成候选", use_container_width=True):
            try:
                st.session_state[candidate_key] = api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/candidates/generate",
                    timeout=120,
                )
            except RuntimeError as exc:
                st.error(str(exc))
        candidates = st.session_state.get(candidate_key)
        if not draft.get("source_snapshot_id"):
            st.warning("该 Draft 未绑定 Metadata Snapshot，无法生成元数据候选。")
        if candidates:
            st.markdown(f"#### Snapshot `{candidates['snapshot_id']}`")
            excluded = [
                {"table": table, "reason": reason}
                for table, reason in candidates["excluded_tables"].items()
            ]
            if excluded:
                st.markdown("##### 被治理策略排除的表")
                st.dataframe(excluded, use_container_width=True, hide_index=True)

            candidate_rows = []
            candidate_labels = {}
            candidate_groups = (
                ("ObjectType", "object_types", "object_type"),
                ("Property", "properties", "property"),
                ("Binding", "bindings", "binding"),
                ("LinkType", "link_types", "link_type"),
                ("PhysicalJoin", "physical_joins", "physical_join"),
            )
            for kind, group_name, payload_name in candidate_groups:
                for item in candidates[group_name]:
                    payload = item[payload_name]
                    resource_id = payload["id"]
                    evidence = "; ".join(
                        f"{entry['source']}: {entry['detail']}"
                        for entry in item["evidence"]
                    )
                    candidate_rows.append(
                        {
                            "candidate_id": item["candidate_id"],
                            "type": kind,
                            "resource_id": resource_id,
                            "confidence": item["confidence"],
                            "evidence": evidence,
                        }
                    )
                    candidate_labels[
                        f"{kind} · {resource_id} · {item['confidence']:.2f}"
                    ] = item["candidate_id"]
            st.dataframe(candidate_rows, use_container_width=True, hide_index=True)
            selected_candidates = st.multiselect(
                "选择要导入 Draft 的候选",
                options=list(candidate_labels),
                key=f"selected_candidates_{draft_id}",
            )
            if st.button(
                "导入所选候选", disabled=not selected_candidates or not editable
            ):
                api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/import-candidates",
                    json={
                        "candidate_ids": [
                            candidate_labels[label] for label in selected_candidates
                        ],
                        "actor": author,
                    },
                    timeout=120,
                )
                st.session_state.pop(candidate_key, None)
                st.rerun()
    with tabs[8]:
        actions = st.columns(4)
        action_specs = [
            ("运行校验", "validate", None, not editable),
            (
                "提交审核",
                "submit",
                {"actor": "ontology-author"},
                not editable or not validation_fresh,
            ),
            (
                "审核通过",
                "approve",
                {"actor": "ontology-reviewer"},
                draft["status"] != "IN_REVIEW",
            ),
        ]
        for column, (label, operation, payload, disabled) in zip(
            actions, action_specs, strict=False
        ):
            if column.button(
                label,
                disabled=disabled,
                type="primary" if operation == "validate" else "secondary",
            ):
                api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/{operation}",
                    json=payload,
                )
                st.rerun()
        report = draft.get("validation_report")
        if report:
            st.success("Dry Run 通过") if report["valid"] else st.error("存在阻断错误")
            st.dataframe(report["issues"], use_container_width=True, hide_index=True)
        with st.form("manager_publish_form"):
            version = st.text_input("新版本号", "object-1.0")
            acknowledge_breaking = st.checkbox("确认已审阅 Breaking Change")
            change_ticket = st.text_input("变更工单（Breaking Change 必填）")
            if st.form_submit_button(
                "原子发布并刷新在线查询",
                type="primary",
                disabled=draft["status"] != "VALIDATED",
            ):
                result = api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/publish",
                    json={
                        "actor": "ontology-reviewer",
                        "version": version,
                        "acknowledge_breaking_changes": acknowledge_breaking,
                        "change_ticket": change_ticket or None,
                    },
                    timeout=180,
                )
                st.success(f"已发布 {result['version']}")
                st.rerun()
        st.markdown("#### 审计时间线")
        audit = api_request(
            "GET", f"/api/v1/ontology/drafts/{draft_id}/audit-events", params={"limit": 100}
        )
        st.dataframe(audit, use_container_width=True, hide_index=True)
        if draft["status"] == "PUBLISHED" and graph.get("version_id"):
            try:
                artifact = api_request(
                    "GET",
                    f"/api/v1/ontology/versions/{graph['version_id']}/compiled-artifact",
                )
            except RuntimeError:
                artifact = None
            if artifact:
                artifact_cols = st.columns(3)
                artifact_cols[0].metric("Artifact", artifact["status"])
                artifact_cols[1].metric("Compiler", artifact["compiler_version"])
                artifact_cols[2].metric("Bundle Hash", artifact["bundle_hash"][:12])
                st.markdown("#### 编译证据")
                st.json(artifact.get("compilation_evidence", artifact["evidence_summary"]))
    with tabs[9]:
        diff = api_request("GET", f"/api/v1/ontology/drafts/{draft_id}/diff")
        impact = api_request("GET", f"/api/v1/ontology/drafts/{draft_id}/impact")
        st.markdown("#### 变更集合")
        change_rows = []
        for key, values in diff.items():
            if isinstance(values, list):
                change_rows.extend(
                    {"group": key, **item} for item in values if isinstance(item, dict)
                )
        st.dataframe(change_rows, use_container_width=True, hide_index=True)
        if impact["breaking_changes"]:
            st.error("存在 Breaking Change，发布时必须确认并填写变更工单。")
        st.markdown("#### 影响分析")
        st.json(impact)
    with tabs[6]:
        st.dataframe(
            resources["physical_joins"], use_container_width=True, hide_index=True
        )
        with st.form("manager_physical_join_form"):
            join_id = st.text_input("Join ID", "transaction_to_branch")
            join_name = st.text_input("名称", "交易连接分行")
            left_table = st.text_input("左表", "dwd_card_transaction")
            left_column = st.text_input("左字段", "branch_id")
            right_table = st.text_input("右表", "dim_branch")
            right_column = st.text_input("右字段", "branch_id")
            relationship = st.selectbox(
                "关系", ["many_to_one", "one_to_one", "one_to_many", "many_to_many"]
            )
            evidence = st.text_area("审核证据", "MiniBank DDL 外键与元数据检查")
            if st.form_submit_button("保存版本化 Physical Join"):
                api_request(
                    "POST",
                    f"/api/v1/ontology/drafts/{draft_id}/physical-joins",
                    json={
                        "id": join_id,
                        "name": join_name,
                        "left_table": left_table,
                        "left_column": left_column,
                        "right_table": right_table,
                        "right_column": right_column,
                        "relationship": relationship,
                        "evidence": [evidence],
                        "lifecycle_status": "ACTIVE",
                    },
                )
                st.rerun()
    with tabs[10]:
        sync_col, index_col = st.columns(2)
        if sync_col.button("运行 Metadata Sync", use_container_width=True):
            sync_col.json(
                api_request("POST", "/api/v1/ontology/sync-runs", timeout=120)
            )
        if index_col.button("构建业务概念索引", use_container_width=True):
            index_col.json(
                api_request(
                    "POST",
                    "/api/v1/ontology/index-builds",
                    json={"index_type": "BUSINESS_CONCEPT"},
                    timeout=120,
                )
            )
        st.markdown("#### Drift 报告")
        st.dataframe(drift_reports, use_container_width=True, hide_index=True)
        st.markdown("#### 版本化索引构建")
        st.dataframe(index_builds, use_container_width=True, hide_index=True)
    with tabs[11]:
        published_object_ids = [item["id"] for item in objects]
        if not published_object_ids:
            st.info("发布对象模型后可使用只读 Object Explorer。")
        else:
            explored_type = st.selectbox("对象类型", published_object_ids)
            explorer_limit = st.slider("返回条数", 1, 100, 20)
            if st.button("加载对象记录", use_container_width=True):
                records = api_request(
                    "GET",
                    f"/api/v1/objects/{explored_type}?limit={explorer_limit}",
                )
                st.session_state["object_explorer_records"] = records
            records = st.session_state.get("object_explorer_records", [])
            st.dataframe(records, use_container_width=True, hide_index=True)
            if records:
                selected_record = st.selectbox(
                    "对象主键", [str(item["primary_key"]) for item in records]
                )
                selected_payload = next(
                    item for item in records if str(item["primary_key"]) == selected_record
                )
                st.json(selected_payload)
                available_links = selected_payload.get("available_links", [])
                if available_links:
                    selected_link = st.selectbox("关系导航", available_links)
                    if st.button("沿 Link 查询"):
                        linked = api_request(
                            "GET",
                            f"/api/v1/objects/{explored_type}/{selected_record}/links/"
                            f"{selected_link}?limit=20",
                        )
                        st.dataframe(linked, use_container_width=True, hide_index=True)


page = st.sidebar.radio(
    "工作台",
    [
        "Text-to-SQL",
        "Ontology Manager",
        "模式对比与评测",
        "认证 SQL 资产",
        "本体构建与审核",
    ],
)
if page == "Text-to-SQL":
    text_to_sql_page()
elif page == "Ontology Manager":
    ontology_manager_page()
elif page == "模式对比与评测":
    evaluation_page()
elif page == "认证 SQL 资产":
    sql_asset_page()
else:
    ontology_builder_page()
