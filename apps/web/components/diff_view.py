from __future__ import annotations

import difflib
import json
from typing import Any

import streamlit as st


def diff_text(before: dict[str, Any], after: dict[str, Any]) -> str:
    return "\n".join(
        difflib.unified_diff(
            json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True).splitlines(),
            json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True).splitlines(),
            fromfile="original",
            tofile="reviewed",
            lineterm="",
        )
    )


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def render_diff(before: dict[str, Any], after: dict[str, Any]) -> None:
    diff = diff_text(before, after)
    st.code(diff or "没有字段变化。", language="diff")
