from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
import streamlit as st
from state import StateKey, get_value, set_value

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


class ApiUnavailable(RuntimeError):
    pass


class DraftRevisionConflict(RuntimeError):
    def __init__(self, message: str, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass
class ApiClient:
    base_url: str = API_BASE_URL

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 120,
        draft_revision: int | None = None,
        actor: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if draft_revision is None:
            draft_revision = get_value(StateKey.DRAFT_REVISION)
        if (
            method.upper() in {"POST", "PUT", "DELETE"}
            and "/ontology/drafts/" in path
            and draft_revision is not None
        ):
            headers["If-Match"] = f'"{draft_revision}"'
        if actor:
            headers["X-Actor"] = actor
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json()
            except ValueError:
                detail = {"detail": exc.response.text or str(exc)}
            if detail.get("error_code") in {
                "DRAFT_REVISION_CONFLICT",
                "DRAFT_CHANGED_DURING_VALIDATION",
                "REVIEW_SNAPSHOT_CHANGED",
            }:
                current = detail.get("current_revision")
                if current is not None:
                    set_value(StateKey.DRAFT_REVISION, current)
                raise DraftRevisionConflict(
                    "Draft 已被其他操作更新，请刷新 Draft 后重新提交。",
                    current_revision=current,
                ) from exc
            message = detail.get("detail") or str(exc)
            if isinstance(message, dict):
                message = message.get("detail") or str(message)
            raise RuntimeError(str(message)) from exc
        except httpx.HTTPError as exc:
            raise ApiUnavailable(f"无法连接 API：{exc}") from exc

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)


def get_client() -> ApiClient:
    return ApiClient()


def handle_api_error(exc: Exception) -> None:
    if isinstance(exc, DraftRevisionConflict):
        st.error(str(exc))
        if exc.current_revision is not None:
            st.info(f"后台当前 Revision：{exc.current_revision}")
    elif isinstance(exc, ApiUnavailable):
        st.error(str(exc))
        st.caption("请确认 FastAPI 容器已启动，或检查 API_BASE_URL / WEB_API_BASE_URL。")
    else:
        st.error(str(exc))


def success(message: str) -> None:
    st.success(message)
