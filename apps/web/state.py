from __future__ import annotations

from enum import StrEnum
from typing import Any

import streamlit as st


class StateKey(StrEnum):
    PAGE = "daa.page"
    WORKBENCH_STEP = "daa.workbench.step"
    SNAPSHOT_ID = "daa.ontology.snapshot_id"
    SNAPSHOT_PAYLOAD = "daa.ontology.snapshot_payload"
    CONSTRUCTION_RUN_ID = "daa.ontology.construction_run_id"
    CANDIDATE_ID = "daa.ontology.candidate_id"
    CURRENT_DRAFT_ID = "daa.ontology.current_draft_id"
    DRAFT_REVISION = "daa.ontology.draft_revision"
    DRAFT_HASH = "daa.ontology.draft_hash"
    LAST_PUBLISHED_VERSION = "daa.ontology.last_published_version"
    QUERY_RESULT = "daa.query.result"
    DEMO_STATUS = "daa.demo.status"
    FORM_BUFFER_PREFIX = "daa.form."


FINAL_CANDIDATE_STATUSES = {"ACCEPTED", "MODIFIED", "REJECTED", "MERGED"}


def get_value(key: StateKey, default: Any = None) -> Any:
    return st.session_state.get(str(key), default)


def set_value(key: StateKey, value: Any) -> None:
    st.session_state[str(key)] = value


def clear_value(key: StateKey) -> None:
    st.session_state.pop(str(key), None)


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
