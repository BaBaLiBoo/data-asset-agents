"""任务三算子级血缘解析专业服务 HTTP 客户端。

血缘服务采用「上传 + 异步任务 + 增量图谱」接口，与任务一、任务二的同步
request/response 不同，因此单独提供客户端。
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from ..contracts import ContractValidationError, validate_contract
from .http_task_agents import DownstreamServiceError


class LineageClient:
    """封装血缘专业服务的脚本上传、任务创建、状态查询和增量图谱。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._token = token
        self._transport = transport
        self._default_headers = default_headers or {}

    async def upload_script(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/lineage/scripts",
            json=payload,
            request_schema="LineageScriptUploadRequest",
            response_schema="LineageScriptUploadResponse",
        )

    async def create_job(self, request: BaseModel) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/lineage/jobs",
            json=request.model_dump(mode="json", by_alias=True),
            request_schema="LineageJobRequest",
            response_schema="LineageJob",
        )

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/lineage/jobs/{job_id}",
            response_schema="LineageJob",
        )

    async def get_graph(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET",
            f"/api/v1/lineage/jobs/{job_id}/graph",
            params=params,
            response_schema="LineageGraphPage",
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        request_schema: str | None = None,
        response_schema: str,
    ) -> dict[str, Any]:
        if request_schema is not None and json is not None:
            try:
                validate_contract(request_schema, json)
            except ContractValidationError as exc:
                raise DownstreamServiceError(
                    code="INVALID_REQUEST",
                    message=f"血缘专业服务请求未通过契约校验：{exc}",
                    retryable=False,
                    http_status=400,
                ) from exc

        headers = {
            "Content-Type": "application/json",
            **self._default_headers,
        }
        if json and json.get("traceId"):
            headers["X-Trace-Id"] = json["traceId"]
        if json and json.get("requestId"):
            headers["X-Request-Id"] = json["requestId"]
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_TIMEOUT",
                message="血缘专业服务响应超时",
                retryable=True,
                http_status=504,
            ) from exc
        except httpx.RequestError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_UNAVAILABLE",
                message=f"无法连接血缘专业服务：{exc}",
                retryable=True,
                http_status=503,
            ) from exc

        if response.status_code >= 400:
            self._raise_http_error(response)

        try:
            result = response.json()
        except ValueError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message="血缘专业服务没有返回合法JSON",
                retryable=False,
            ) from exc

        try:
            validate_contract(response_schema, result)
        except ContractValidationError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message=f"血缘专业服务响应未通过契约校验：{exc}",
                retryable=False,
            ) from exc
        return result

    @staticmethod
    def _raise_http_error(response: httpx.Response) -> None:
        code = "DOWNSTREAM_UNAVAILABLE"
        message = f"血缘专业服务返回HTTP {response.status_code}"
        retryable = response.status_code in {502, 503, 504}
        try:
            payload = response.json()
            error = payload.get("error", {})
            code = error.get("code", code)
            message = error.get("message", message)
            retryable = bool(error.get("retryable", retryable))
        except ValueError:
            pass
        raise DownstreamServiceError(
            code=code,
            message=message,
            retryable=retryable,
            http_status=response.status_code,
        )
