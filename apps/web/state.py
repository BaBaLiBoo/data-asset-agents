from __future__ import annotations

from enum import StrEnum
from typing import Any

import streamlit as st


class StateKey(StrEnum):
    CURRENT_PAGE = "daa.page"
    PENDING_PAGE = "daa.pending_page"
    SELECTED_DATA_SOURCE = "daa.selected_data_source"
    SELECTED_ONTOLOGY_VERSION = "daa.selected_ontology_version"
    SNAPSHOT_ID = "daa.ontology.snapshot_id"
    SNAPSHOT_PAYLOAD = "daa.ontology.snapshot_payload"
    CONSTRUCTION_RUN_ID = "daa.ontology.construction_run_id"
    CURRENT_CANDIDATE_ID = "daa.ontology.candidate_id"
    CURRENT_DRAFT_ID = "daa.ontology.current_draft_id"
    DRAFT_REVISION = "daa.ontology.draft_revision"
    DRAFT_HASH = "daa.ontology.draft_hash"
    BUILD_STEP = "daa.ontology.build_step"
    CHAT_MESSAGES = "daa.chat.messages"
    LAST_QUERY_RESULT = "daa.query.result"
    AI_STATUS = "daa.ai.status"
    FORM_BUFFER_PREFIX = "daa.form."
    # Compatibility aliases for hidden legacy Streamlit functions.
    WORKBENCH_STEP = BUILD_STEP
    CANDIDATE_ID = CURRENT_CANDIDATE_ID
    LAST_PUBLISHED_VERSION = SELECTED_ONTOLOGY_VERSION
    QUERY_RESULT = LAST_QUERY_RESULT
    DEMO_STATUS = "daa.demo.status"


FINAL_CANDIDATE_STATUSES = {"ACCEPTED", "MODIFIED", "REJECTED", "MERGED"}


def get_value(key: StateKey, default: Any = None) -> Any:
    return st.session_state.get(str(key), default)


def set_value(key: StateKey, value: Any) -> None:
    st.session_state[str(key)] = value


def clear_value(key: StateKey) -> None:
    st.session_state.pop(str(key), None)


def request_navigation(page: str) -> None:
    st.session_state[str(StateKey.PENDING_PAGE)] = page


def apply_pending_navigation(allowed_pages: list[str]) -> str:
    pending = st.session_state.pop(str(StateKey.PENDING_PAGE), None)
    current = st.session_state.get(str(StateKey.CURRENT_PAGE))
    if pending in allowed_pages:
        current = pending
    if current not in allowed_pages:
        current = allowed_pages[0]
    st.session_state[str(StateKey.CURRENT_PAGE)] = current
    return current


def remember_draft(draft: dict[str, Any]) -> None:
    set_value(StateKey.CURRENT_DRAFT_ID, draft["id"])
    set_value(StateKey.DRAFT_REVISION, draft["resource_revision"])
    set_value(StateKey.DRAFT_HASH, draft.get("resource_hash", ""))


def current_draft_headers() -> dict[str, str]:
    revision = get_value(StateKey.DRAFT_REVISION)
    if revision is None:
        return {}
    return {"If-Match": f'"{revision}"'}


def completion_ratio(candidates: list[dict[str, Any]]) -> float:
    actionable = [item for item in candidates if item["status"] != "DEFERRED"]
    if not actionable:
        return 0.0
    completed = sum(item["status"] in FINAL_CANDIDATE_STATUSES for item in actionable)
    return completed / len(actionable)


def candidate_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": len(candidates),
        "accepted": 0,
        "modified": 0,
        "rejected": 0,
        "merged": 0,
        "deferred": 0,
        "pending": 0,
    }
    mapping = {
        "ACCEPTED": "accepted",
        "MODIFIED": "modified",
        "REJECTED": "rejected",
        "MERGED": "merged",
        "DEFERRED": "deferred",
        "PENDING": "pending",
    }
    for item in candidates:
        key = mapping.get(item.get("status", "PENDING"), "pending")
        counts[key] += 1
    return counts
