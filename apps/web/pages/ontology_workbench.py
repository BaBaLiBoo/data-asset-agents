from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st
from api_client import get_client, handle_api_error, success
from components.diff_view import changed_fields, render_diff
from components.evidence import render_evidence
from components.resource_forms import RESOURCE_LABELS, render_resource_form
from components.status import render_resource_counts
from components.workflow_stepper import WORKFLOW_STEPS, render_stepper
from state import (
    FINAL_CANDIDATE_STATUSES,
    StateKey,
    candidate_counts,
    completion_ratio,
    get_value,
    remember_draft,
    request_navigation,
    set_value,
)

RESOURCE_ENDPOINTS = {
    "object_type": "object-types",
    "property": "properties",
    "metric": "metrics",
    "dimension": "dimensions",
    "binding": "bindings",
    "link_type": "link-types",
    "physical_join": "physical-joins",
}


def _developer_mode() -> bool:
    return os.getenv("DEVELOPER_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _table_columns(table_assets: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        item["name"]: item.get("columns", [])
        for item in table_assets
        if item.get("status") == "ACTIVE" and item.get("selectable", True)
    }


def _resource_options(resources: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    return {
        "object_ids": [item["id"] for item in resources.get("object_types", [])],
        "property_ids": [item["id"] for item in resources.get("properties", [])],
        "dimension_ids": [item["id"] for item in resources.get("dimensions", [])],
        "join_ids": [item["id"] for item in resources.get("physical_joins", [])],
    }


def _current_step(
    run: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    draft: dict[str, Any] | None,
) -> tuple[str, set[str]]:
    completed: set[str] = set()
    if get_value(StateKey.SNAPSHOT_ID):
        completed.add("数据源扫描")
    if run and run.get("status") not in {"CREATED", "FAILED"}:
        completed.add("自动构建")
    if candidates and all(item["status"] in FINAL_CANDIDATE_STATUSES for item in candidates):
        completed.add("候选审核")
    if draft:
        completed.add("草稿完善")
    if draft and draft.get("status") in {"PUBLISHED", "VALIDATED"}:
        completed.add("校验发布")
    if draft and draft.get("status") == "PUBLISHED":
        completed.add("完成")
    for step in WORKFLOW_STEPS:
        if step not in completed:
            return step, completed
    return "完成", completed


def _load_run_and_candidates() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    client = get_client()
    run_id = get_value(StateKey.CONSTRUCTION_RUN_ID)
    run = None
    candidates: list[dict[str, Any]] = []
    if run_id:
        try:
            run = client.get(f"/api/v1/ontology/construction-runs/{run_id}")
            if run.get("status") not in {"CREATED", "FAILED"}:
                candidates = client.get(f"/api/v1/ontology/construction-runs/{run_id}/candidates")
            if run.get("promoted_draft_id") and not get_value(StateKey.CURRENT_DRAFT_ID):
                set_value(StateKey.CURRENT_DRAFT_ID, run["promoted_draft_id"])
        except Exception as exc:
            handle_api_error(exc)
    return run, candidates


def _load_draft() -> dict[str, Any] | None:
    draft_id = get_value(StateKey.CURRENT_DRAFT_ID)
    if not draft_id:
        return None
    try:
        detail = get_client().get(f"/api/v1/ontology/drafts/{draft_id}")
        remember_draft(detail["draft"])
        return detail
    except Exception as exc:
        handle_api_error(exc)
        return None


def _snapshot_page() -> None:
    client = get_client()
    st.subheader("1. 数据源扫描")
    try:
        sources = client.get("/api/v1/ontology/data-sources")
    except Exception as exc:
        handle_api_error(exc)
        sources = []
    if sources:
        source = sources[0]
        cols = st.columns(5)
        cols[0].metric("数据源", source["name"])
        cols[1].metric("Provider", source["provider"])
        cols[2].metric("连接引用", source["connection_ref"])
        cols[3].metric("启用", "是" if source["enabled"] else "否")
        if cols[4].button("检查数据源", use_container_width=True):
            try:
                inspection = client.post(f"/api/v1/ontology/data-sources/{source['id']}/inspect")
                st.session_state["daa.datasource.inspection"] = inspection
            except Exception as exc:
                handle_api_error(exc)
        inspection = st.session_state.get("daa.datasource.inspection")
        if inspection:
            st.dataframe([inspection], use_container_width=True, hide_index=True)
    else:
        st.warning("未配置数据源。")

    cols = st.columns([2, 1, 1])
    schema_name = cols[0].text_input("Schema", "public")
    sample_limit = cols[1].number_input("样例行数", 1, 10, 5)
    top_limit = cols[2].number_input("Top Values", 1, 20, 5)
    if st.button("创建 RAW_METADATA Snapshot", type="primary", use_container_width=True):
        try:
            build = client.post(
                "/api/v1/ontology/metadata-snapshots/raw",
                json={
                    "schema_name": schema_name,
                    "sample_limit": sample_limit,
                    "top_value_limit": top_limit,
                },
                timeout=180,
            )
            snapshot = build["snapshot"]
            set_value(StateKey.SNAPSHOT_ID, snapshot["id"])
            set_value(StateKey.SNAPSHOT_PAYLOAD, build)
            success("RAW_METADATA Snapshot 已创建。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    build = get_value(StateKey.SNAPSHOT_PAYLOAD)
    if not build and get_value(StateKey.SNAPSHOT_ID):
        st.info(f"当前 Snapshot：{get_value(StateKey.SNAPSHOT_ID)}")
    if build:
        snapshot = build["snapshot"]
        st.markdown("#### Snapshot 摘要")
        cols = st.columns(4)
        cols[0].metric("Snapshot", snapshot["id"])
        cols[1].metric("表数量", len(snapshot.get("tables", [])))
        cols[2].metric("Snapshot Hash", (snapshot.get("snapshot_hash") or "")[:12])
        cols[3].metric("捕获时间", snapshot.get("captured_at", ""))
        table_rows = [
            {
                "表": table["table_name"],
                "字段数": len(table.get("columns", [])),
                "PK": ", ".join(table.get("primary_key", [])),
                "FK": len(table.get("foreign_keys", [])),
                "Index": len(table.get("indexes", [])),
                "Profiling": len(table.get("profiles", [])),
            }
            for table in snapshot.get("tables", [])
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
        with st.expander("查看表字段、PK、FK、Index 和 Profiling"):
            st.json(
                [
                    {
                        "table": table["table_name"],
                        "columns": table.get("columns", []),
                        "primary_key": table.get("primary_key", []),
                        "foreign_keys": table.get("foreign_keys", []),
                        "indexes": table.get("indexes", []),
                        "profiles": table.get("profiles", []),
                    }
                    for table in snapshot.get("tables", [])
                ]
            )
        with st.expander("查看原始元数据"):
            st.json(build)


def _construction_page(run: dict[str, Any] | None) -> None:
    client = get_client()
    st.subheader("2. 自动构建")
    st.write(
        "O-A：仅元数据结构；O-B：元数据和字段画像；"
        "O-C：元数据、画像和认证历史 SQL；O-D：O-C 加大模型语义建议。"
    )
    snapshot_id = st.text_input("RAW_METADATA Snapshot", get_value(StateKey.SNAPSHOT_ID, ""))
    cols = st.columns(3)
    evidence_mode = cols[0].selectbox("构建模式", ["O-A", "O-B", "O-C", "O-D"], index=2)
    catalog_mode = cols[1].selectbox("Catalog Mode", ["RAW_METADATA", "GOVERNED_CATALOG"], index=1)
    llm_mode = cols[2].selectbox("LLM Mode", ["mock", "live"], disabled=evidence_mode != "O-D")
    if st.button("创建 Construction Run", disabled=not snapshot_id, type="primary"):
        try:
            created = client.post(
                "/api/v1/ontology/construction-runs",
                json={
                    "source_snapshot_id": snapshot_id,
                    "catalog_mode": catalog_mode,
                    "construction_mode": "STRICT_CONSTRUCTION",
                    "evidence_mode": evidence_mode,
                    "llm_mode": llm_mode if evidence_mode == "O-D" else "mock",
                    "provider": "configured-live" if llm_mode == "live" else "mock",
                    "model": "configured-live" if llm_mode == "live" else "deterministic-rules-v1",
                    "temperature": 0,
                    "random_seed": 20260715,
                    "created_by": "streamlit-reviewer",
                },
                timeout=60,
            )
            set_value(StateKey.CONSTRUCTION_RUN_ID, created["run_id"])
            success("Construction Run 已创建。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    try:
        runs = client.get("/api/v1/ontology/construction-runs")
    except Exception as exc:
        handle_api_error(exc)
        runs = []
    if runs:
        labels = {
            f"{item['run_id']} · {item['evidence_mode']} · {item['status']}": item["run_id"]
            for item in runs
        }
        selected = st.selectbox(
            "选择已有 Run",
            list(labels),
            index=0,
        )
        if labels[selected] != get_value(StateKey.CONSTRUCTION_RUN_ID) and st.button(
            "打开所选 Run"
        ):
            set_value(StateKey.CONSTRUCTION_RUN_ID, labels[selected])
            st.rerun()
    if run:
        st.markdown("#### Run 状态")
        cols = st.columns(8)
        cols[0].metric("Run ID", run["run_id"])
        cols[1].metric("状态", run["status"])
        cols[2].metric("Evidence", run["evidence_mode"])
        cols[3].metric("Catalog", run["catalog_mode"])
        cols[4].metric("LLM", run["llm_mode"])
        cols[5].metric("候选数", sum(run.get("candidate_counts", {}).values()))
        cols[6].metric("排除表", len(run.get("excluded_tables", {})))
        cols[7].metric("Warning", len(run.get("warnings", [])))
        if run.get("excluded_tables"):
            st.dataframe(
                [{"表": key, "原因": value} for key, value in run["excluded_tables"].items()],
                use_container_width=True,
                hide_index=True,
            )
        if run.get("warnings"):
            st.warning("\n".join(run["warnings"]))
        if run.get("errors"):
            st.error("\n".join(run["errors"]))
        if run["status"] == "CREATED" and st.button(
            "生成候选", type="primary", use_container_width=True
        ):
            try:
                client.post(
                    f"/api/v1/ontology/construction-runs/{run['run_id']}/generate", timeout=180
                )
                success("候选已生成。")
                st.rerun()
            except Exception as exc:
                handle_api_error(exc)


def _candidate_page(run: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    client = get_client()
    st.subheader("3. 候选审核")
    counts = candidate_counts(candidates)
    cols = st.columns(7)
    for col, label, key in zip(
        cols,
        ["总候选", "已接受", "已修改", "已拒绝", "已合并", "已延期", "待处理"],
        ["total", "accepted", "modified", "rejected", "merged", "deferred", "pending"],
        strict=False,
    ):
        col.metric(label, counts[key])
    st.progress(completion_ratio(candidates), text=f"审核完成度 {completion_ratio(candidates):.0%}")

    filters = st.columns(5)
    type_filter = filters[0].selectbox("资源类型", ["全部", *sorted(RESOURCE_LABELS)])
    status_filter = filters[1].selectbox(
        "状态", ["全部", "PENDING", "ACCEPTED", "MODIFIED", "REJECTED", "MERGED", "DEFERRED"]
    )
    min_confidence = filters[2].slider("最低置信度", 0.0, 1.0, 0.0, 0.05)
    llm_only = filters[3].checkbox("包含 LLM 建议")
    warning_only = filters[4].checkbox("有冲突或 Warning")
    filtered = []
    for item in candidates:
        confidence = max([ev.get("confidence", 0) for ev in item.get("evidence", [])] or [1.0])
        has_llm = any(ev.get("llm_generated") for ev in item.get("evidence", []))
        has_warning = bool(item.get("current_resource", {}).get("warnings") or item.get("warnings"))
        if type_filter != "全部" and item["resource_type"] != type_filter:
            continue
        if status_filter != "全部" and item["status"] != status_filter:
            continue
        if confidence < min_confidence:
            continue
        if llm_only and not has_llm:
            continue
        if warning_only and not has_warning:
            continue
        filtered.append(item)
    if not filtered:
        st.info("当前筛选条件下没有候选。")
        return
    selected_id = get_value(StateKey.CANDIDATE_ID) or filtered[0]["candidate_id"]
    ids = [item["candidate_id"] for item in filtered]
    if selected_id not in ids:
        selected_id = ids[0]
    selected_id = st.selectbox(
        "候选项",
        ids,
        index=ids.index(selected_id),
        format_func=lambda cid: next(
            f"{item['resource_type']} · {item['current_resource'].get('id')} · {item['status']}"
            for item in filtered
            if item["candidate_id"] == cid
        ),
    )
    set_value(StateKey.CANDIDATE_ID, selected_id)
    candidate = next(item for item in filtered if item["candidate_id"] == selected_id)
    original = next(
        (
            value
            for value in candidate["original_candidate"].values()
            if isinstance(value, dict) and value.get("id")
        ),
        candidate["current_resource"],
    )
    readonly = candidate["status"] in FINAL_CANDIDATE_STATUSES
    left, middle, right = st.columns([1, 1.35, 1])
    with left:
        st.markdown("#### 物理证据")
        render_evidence(candidate.get("evidence", []))
        with st.expander("原始候选"):
            st.json(candidate["original_candidate"])
    with middle:
        st.markdown("#### 结构化编辑表单")
        edited = render_resource_form(
            candidate["resource_type"],
            candidate["current_resource"],
            disabled=readonly,
            key_prefix=f"candidate.{candidate['candidate_id']}",
        )
        if _developer_mode():
            with st.expander("高级 JSON 编辑"):
                raw_json = st.text_area(
                    "修改后的 JSON",
                    json.dumps(edited, ensure_ascii=False, indent=2),
                    height=220,
                    disabled=readonly,
                )
                try:
                    edited = json.loads(raw_json)
                except json.JSONDecodeError as exc:
                    st.error(f"JSON 格式错误：{exc}")
    with right:
        st.markdown("#### 最终资源预览与 Diff")
        st.json(edited)
        render_diff(original, edited)
        st.write("修改字段：", ", ".join(changed_fields(original, edited)) or "无")

    reviewer = st.text_input("审核人", "streamlit-reviewer")
    comment = st.text_area("审核说明", candidate.get("comment", ""))
    target_options = [
        item["candidate_id"]
        for item in candidates
        if item["candidate_id"] != candidate["candidate_id"]
        and item["resource_type"] == candidate["resource_type"]
    ]
    decision = st.radio(
        "审核操作", ["ACCEPT", "MODIFY", "REJECT", "MERGE", "DEFER"], horizontal=True
    )
    merge_target = None
    if decision == "MERGE":
        merge_target = st.selectbox("合并目标", target_options)
    cols = st.columns(4)
    if cols[0].button("上一个", disabled=ids.index(selected_id) == 0):
        set_value(StateKey.CANDIDATE_ID, ids[ids.index(selected_id) - 1])
        st.rerun()
    if cols[1].button("下一个", disabled=ids.index(selected_id) == len(ids) - 1):
        set_value(StateKey.CANDIDATE_ID, ids[ids.index(selected_id) + 1])
        st.rerun()
    if cols[2].button("重新加载最新状态"):
        st.rerun()
    submit_disabled = readonly
    if decision == "MODIFY" and not changed_fields(candidate["current_resource"], edited):
        submit_disabled = True
        st.warning("MODIFY 必须至少修改一个字段。")
    if decision == "MERGE" and not merge_target:
        submit_disabled = True
        st.warning("MERGE 必须选择有效目标。")
    if decision == "REJECT" and not comment:
        submit_disabled = True
        st.info("REJECT 必须填写拒绝原因，建议说明业务理由。")
    if cols[3].button("提交审核", type="primary", disabled=submit_disabled):
        if edited.get("id") != candidate["current_resource"].get("id"):
            st.error("Stable ID 不允许修改。")
            return
        try:
            client.post(
                (
                    f"/api/v1/ontology/construction-runs/{run['run_id']}/candidates/"
                    f"{candidate['candidate_id']}/review"
                ),
                json={
                    "decision": decision,
                    "reviewer": reviewer,
                    "modified_resource": edited if decision == "MODIFY" else None,
                    "comment": comment,
                    "merge_target_candidate_id": merge_target,
                },
            )
            success("审核决定已保存。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    unresolved = sum(item["status"] not in FINAL_CANDIDATE_STATUSES for item in candidates)
    if unresolved == 0 and st.button(
        "生成可编辑本体草稿", type="primary", use_container_width=True
    ):
        try:
            detail = client.post(
                f"/api/v1/ontology/construction-runs/{run['run_id']}/promote-to-draft",
                json={
                    "draft_name": f"Reviewed {run['evidence_mode']} ontology",
                    "actor": reviewer,
                },
                timeout=180,
            )
            remember_draft(detail["draft"])
            success("已生成可编辑本体草稿，并自动打开草稿完善步骤。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)


def _resource_editor(
    draft_id: str,
    detail: dict[str, Any],
    resource_type: str,
    table_assets: list[dict[str, Any]],
    editable: bool,
) -> None:
    client = get_client()
    resources = detail["resources"]
    options = _resource_options(resources)
    collection = {
        "object_type": "object_types",
        "property": "properties",
        "metric": "metrics",
        "dimension": "dimensions",
        "binding": "bindings",
        "link_type": "link_types",
        "physical_join": "physical_joins",
    }[resource_type]
    existing = resources.get(collection, [])
    st.dataframe(existing, use_container_width=True, hide_index=True)
    mode = st.radio(f"{RESOURCE_LABELS[resource_type]} 操作", ["新增", "编辑"], horizontal=True)
    selected = None
    if mode == "编辑" and existing:
        selected_id = st.selectbox("选择资源", [item["id"] for item in existing])
        selected = next(item for item in existing if item["id"] == selected_id)
    base = selected or {"id": st.text_input("新资源 ID", f"new_{resource_type}")}
    edited = render_resource_form(
        resource_type,
        base,
        object_ids=options["object_ids"],
        property_ids=options["property_ids"],
        dimension_ids=options["dimension_ids"],
        join_ids=options["join_ids"],
        table_columns=_table_columns(table_assets),
        disabled=not editable,
        key_prefix=f"draft.{resource_type}.{base.get('id')}",
    )
    cols = st.columns(2)
    if cols[0].button("保存", type="primary", disabled=not editable):
        try:
            endpoint = RESOURCE_ENDPOINTS[resource_type]
            if selected:
                updated = client.put(
                    f"/api/v1/ontology/drafts/{draft_id}/{endpoint}/{selected['id']}",
                    json=edited,
                    actor="ontology-author",
                )
            else:
                updated = client.post(
                    f"/api/v1/ontology/drafts/{draft_id}/{endpoint}",
                    json=edited,
                    actor="ontology-author",
                )
            remember_draft(updated["draft"])
            success("资源已保存，Validation 已过期。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    if selected and cols[1].button("删除", disabled=not editable):
        try:
            endpoint = RESOURCE_ENDPOINTS[resource_type]
            updated = client.delete(
                f"/api/v1/ontology/drafts/{draft_id}/{endpoint}/{selected['id']}",
                actor="ontology-author",
            )
            remember_draft(updated["draft"])
            success("资源已删除，Validation 已过期。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)


def _draft_page(detail: dict[str, Any]) -> None:
    client = get_client()
    draft = detail["draft"]
    resources = detail["resources"]
    draft_id = draft["id"]
    editable = draft["status"] == "DRAFT"
    st.subheader("4. 草稿完善")
    cols = st.columns(5)
    cols[0].metric("Draft ID", draft_id)
    cols[1].metric("Resource Revision", draft["resource_revision"])
    cols[2].metric("Resource Hash", draft["resource_hash"][:12])
    cols[3].metric("状态", draft["status"])
    cols[4].metric("来源 Run", draft.get("construction_run_id") or "无")
    render_resource_counts(
        {
            "object_types": len(resources.get("object_types", [])),
            "properties": len(resources.get("properties", [])),
            "metrics": len(resources.get("metrics", [])),
            "dimensions": len(resources.get("dimensions", [])),
            "link_types": len(resources.get("link_types", [])),
            "bindings": len(resources.get("bindings", [])),
            "physical_joins": len(resources.get("physical_joins", [])),
        }
    )
    validation_fresh = (
        draft["validation_state"] == "VALID"
        and draft.get("validated_revision") == draft["resource_revision"]
        and draft.get("validated_hash") == draft["resource_hash"]
    )
    if editable and not validation_fresh:
        st.warning("当前 Draft 存在未校验修改。")
    bound = {
        prop
        for binding in resources.get("bindings", [])
        for prop in (binding.get("property_bindings") or {})
    }
    properties = resources.get("properties", [])
    coverage = len(bound) / len(properties) if properties else 0
    primary_props = {
        item.get("primary_key_property_id")
        for item in resources.get("object_types", [])
        if item.get("primary_key_property_id")
    }
    st.metric("绑定覆盖率", f"{coverage:.0%}")
    st.metric("主键绑定完整性", "完整" if primary_props <= bound else "待补齐")
    st.write("未映射 Property：", ", ".join(sorted({p["id"] for p in properties} - bound)) or "无")
    try:
        table_assets = client.get("/api/v1/ontology/tables")
    except Exception:
        table_assets = []
    tabs = st.tabs(
        [
            "对象",
            "属性",
            "指标",
            "维度",
            "业务关系",
            "数据源绑定",
            "Physical Join",
            "对象图",
            "变更 Diff",
            "影响分析",
        ]
    )
    for tab, resource_type in zip(
        tabs[:7],
        ["object_type", "property", "metric", "dimension", "link_type", "binding", "physical_join"],
        strict=False,
    ):
        with tab:
            _resource_editor(draft_id, detail, resource_type, table_assets, editable)
    with tabs[7]:
        nodes = resources.get("object_types", [])
        links = resources.get("link_types", [])
        if nodes:
            dot = ["digraph draft {", "rankdir=LR;"]
            dot += [f'"{node["id"]}" [label="{node["name"]}"];' for node in nodes]
            dot += [
                f'"{link["source_object_type_id"]}" -> "{link["target_object_type_id"]}" '
                f'[label="{link["name"]}"];'
                for link in links
            ]
            dot.append("}")
            st.graphviz_chart("\n".join(dot), use_container_width=True)
        else:
            st.info("当前 Draft 尚无对象。")
    with tabs[8]:
        try:
            diff = client.get(f"/api/v1/ontology/drafts/{draft_id}/diff")
            st.json(diff)
        except Exception as exc:
            handle_api_error(exc)
    with tabs[9]:
        try:
            impact = client.get(f"/api/v1/ontology/drafts/{draft_id}/impact")
            st.json(impact)
        except Exception as exc:
            handle_api_error(exc)


def _publish_page(detail: dict[str, Any]) -> None:
    client = get_client()
    draft = detail["draft"]
    draft_id = draft["id"]
    st.subheader("5. 校验、审核与发布")
    validation_fresh = (
        draft["validation_state"] == "VALID"
        and draft.get("validated_revision") == draft["resource_revision"]
        and draft.get("validated_hash") == draft["resource_hash"]
    )
    cols = st.columns(8)
    cols[0].metric("Draft Status", draft["status"])
    cols[1].metric("Revision", draft["resource_revision"])
    cols[2].metric("Hash", draft["resource_hash"][:12])
    cols[3].metric("Validation", draft["validation_state"])
    cols[4].metric("Validated Revision", draft.get("validated_revision") or "无")
    cols[5].metric("Submitted Revision", draft.get("submitted_revision") or "无")
    report = draft.get("validation_report") or {}
    cols[6].metric("SQLGlot", "PASS" if report.get("valid") else "未知")
    cols[7].metric("EXPLAIN", report.get("explain_passed"))
    if report:
        st.markdown("#### Validation Report")
        st.dataframe(report.get("issues", []), use_container_width=True, hide_index=True)
        with st.expander("Dynamic Dry Run"):
            st.json(report.get("dry_run_cases", []))
    actions = st.columns(5)
    if actions[0].button("Validate", type="primary", disabled=draft["status"] != "DRAFT"):
        try:
            updated = client.post(
                f"/api/v1/ontology/drafts/{draft_id}/validate", actor="ontology-author"
            )
            remember_draft(updated["draft"])
            success("Validate 完成。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    if actions[1].button("Submit", disabled=not validation_fresh or draft["status"] != "DRAFT"):
        try:
            updated = client.post(
                f"/api/v1/ontology/drafts/{draft_id}/submit",
                json={"actor": "ontology-author"},
            )
            remember_draft(updated["draft"])
            success("Draft 已提交审核。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    if actions[2].button("Approve", disabled=draft["status"] != "IN_REVIEW"):
        try:
            updated = client.post(
                f"/api/v1/ontology/drafts/{draft_id}/approve",
                json={"actor": "ontology-reviewer"},
            )
            remember_draft(updated["draft"])
            success("Draft 已审核通过。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    try:
        impact = client.get(f"/api/v1/ontology/drafts/{draft_id}/impact")
    except Exception:
        impact = {"breaking_changes": []}
    breaking = bool(impact.get("breaking_changes"))
    if breaking:
        st.error("存在 Breaking Change，发布前必须确认并填写变更工单。")
        st.json(impact["breaking_changes"])
    acknowledge = st.checkbox("确认 Breaking Change 已评审", disabled=not breaking)
    ticket = st.text_input("变更工单", placeholder="Breaking Change 必填")
    version_name = st.text_input("发布 Version Name", "minibank-demo-draft-v1")
    publish_disabled = draft["status"] != "VALIDATED" or (
        breaking and (not acknowledge or not ticket)
    )
    if st.button("Publish", type="primary", disabled=publish_disabled, use_container_width=True):
        try:
            version = client.post(
                f"/api/v1/ontology/drafts/{draft_id}/publish",
                json={
                    "actor": "ontology-reviewer",
                    "version": version_name,
                    "description": "Streamlit Ontology Workbench publication",
                    "acknowledge_breaking_changes": acknowledge,
                    "change_ticket": ticket or None,
                },
                timeout=180,
            )
            set_value(StateKey.LAST_PUBLISHED_VERSION, version["version"])
            success(f"已发布 {version['version']}。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    last_version = get_value(StateKey.LAST_PUBLISHED_VERSION)
    if last_version and st.button("设为当前在线本体并进入智能问数", use_container_width=True):
        try:
            client.post(f"/api/v1/ontology/versions/{last_version}/activate", timeout=180)
            request_navigation("智能体 Demo")
            success("在线 OntologyService 已切换。")
            st.rerun()
        except Exception as exc:
            handle_api_error(exc)
    try:
        audit = client.get(
            f"/api/v1/ontology/drafts/{draft_id}/audit-events", params={"limit": 100}
        )
        st.markdown("#### Audit Events")
        st.dataframe(audit, use_container_width=True, hide_index=True)
    except Exception as exc:
        handle_api_error(exc)


def _version_management() -> None:
    client = get_client()
    st.subheader("6. 完成与版本管理")
    try:
        status = client.get("/api/v1/ontology/demo-status", timeout=30)
        versions = client.get("/api/v1/ontology/versions")
    except Exception as exc:
        handle_api_error(exc)
        return
    configured = status.get("configured_demo_version") or {}
    rows = []
    for item in versions:
        source = (
            "HUMAN_REVIEWED_OFFICIAL"
            if item.get("version") == configured.get("version")
            else (
                "LLM_REVIEWED_O_D" if "O-D" in item.get("description", "") else "AUTO_REVIEWED_O_C"
            )
        )
        artifact = None
        try:
            artifact = client.get(f"/api/v1/ontology/versions/{item['id']}/compiled-artifact")
        except Exception:
            artifact = {}
        rows.append(
            {
                "Display Name": status.get("display_name")
                if item.get("version") == configured.get("version")
                else item.get("version"),
                "Version Name": item.get("version"),
                "Version ID": item.get("id"),
                "Source": source,
                "Construction Run": artifact.get("construction_run_id"),
                "Published At": item.get("published_at"),
                "Published By": item.get("published_by"),
                "Artifact": artifact.get("status"),
                "Bundle Hash": (artifact.get("bundle_hash") or "")[:12],
                "Ontology Index": (status.get("ontology_index") or {}).get("status"),
                "SQLAsset Build": "READY" if status.get("sql_asset_build") else "MISSING",
                "Current": item.get("is_current"),
                "Resource Counts": {
                    "concepts": item.get("concept_count"),
                    "mappings": item.get("mapping_count"),
                    "joins": item.get("join_count"),
                },
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    inactive = [item for item in versions if not item.get("is_current")]
    if inactive:
        selected = st.selectbox("激活或回滚版本", [item["version"] for item in inactive])
        confirm = st.checkbox("确认切换当前在线本体")
        if st.button("激活所选版本", disabled=not confirm):
            try:
                client.post(f"/api/v1/ontology/versions/{selected}/activate", timeout=180)
                success("版本已激活，OntologyService、Index 和 SQLAsset 已刷新。")
                st.rerun()
            except Exception as exc:
                handle_api_error(exc)


def render() -> None:
    st.title("Ontology Workbench")
    run, candidates = _load_run_and_candidates()
    draft_detail = _load_draft()
    current_step, completed = _current_step(
        run, candidates, draft_detail["draft"] if draft_detail else None
    )
    render_stepper(completed, current_step)
    tabs = st.tabs(WORKFLOW_STEPS)
    with tabs[0]:
        _snapshot_page()
    with tabs[1]:
        _construction_page(run)
    with tabs[2]:
        if run and candidates:
            _candidate_page(run, candidates)
        else:
            st.info("请先完成自动构建并生成候选。")
    with tabs[3]:
        if draft_detail:
            _draft_page(draft_detail)
        else:
            st.info("候选全部审核完成后，可以生成可编辑本体草稿。")
    with tabs[4]:
        if draft_detail:
            _publish_page(draft_detail)
        else:
            st.info("请先生成或打开 Draft。")
    with tabs[5]:
        _version_management()
