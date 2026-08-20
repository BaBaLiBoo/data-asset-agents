"""通过 HTTPX 调用任务一和任务二真实专业服务。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel

from ..agents.task_agents import TaskAgentPort
from ..contracts import ContractValidationError, validate_contract


class DownstreamServiceError(RuntimeError):
    """统一表示专业服务网络、HTTP、转换或契约错误。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        http_status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status


class HttpTaskAgent(TaskAgentPort, ABC):
    """把总控统一契约转换为真实专业服务契约。"""

    def __init__(
        self,
        *,
        base_url: str,
        endpoint: str,
        request_schema: str,
        response_schema: str,
        timeout_seconds: float,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoint = endpoint
        self._request_schema = request_schema
        self._response_schema = response_schema
        self._timeout_seconds = timeout_seconds
        self._token = token
        self._transport = transport
        self._default_headers = default_headers or {}

    async def execute(self, request: BaseModel) -> dict[str, Any]:
        supervisor_payload = request.model_dump(mode="json", by_alias=True)
        self._validate_request(supervisor_payload)
        downstream_payload = self.build_downstream_payload(supervisor_payload)
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": supervisor_payload["traceId"],
            "X-Request-Id": supervisor_payload["requestId"],
            **self._default_headers,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        started = perf_counter()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    json=downstream_payload,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_TIMEOUT",
                message="专业服务响应超时",
                retryable=True,
                http_status=504,
            ) from exc
        except httpx.RequestError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_UNAVAILABLE",
                message=f"无法连接专业服务：{exc}",
                retryable=True,
                http_status=503,
            ) from exc

        latency_ms = max(0, round((perf_counter() - started) * 1000))
        if response.status_code >= 400:
            normalized_error = self.normalize_http_error(
                supervisor_payload, response, latency_ms
            )
            if normalized_error is not None:
                return self._validate_response(normalized_error)
            self._raise_http_error(response)

        try:
            downstream_response = response.json()
        except ValueError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message="专业服务没有返回合法JSON",
                retryable=False,
            ) from exc
        if not isinstance(downstream_response, dict):
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message="专业服务响应必须是JSON对象",
                retryable=False,
            )

        try:
            normalized = self.normalize_response(
                supervisor_payload, downstream_response, latency_ms
            )
        except DownstreamServiceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message=f"专业服务响应转换失败：{exc}",
                retryable=False,
            ) from exc
        return self._validate_response(normalized)

    def _validate_request(self, payload: dict[str, Any]) -> None:
        try:
            validate_contract(self._request_schema, payload)
        except ContractValidationError as exc:
            raise DownstreamServiceError(
                code="INVALID_REQUEST",
                message=f"专业服务请求未通过总控契约校验：{exc}",
                retryable=False,
                http_status=400,
            ) from exc

    def _validate_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_contract(self._response_schema, payload)
        except ContractValidationError as exc:
            raise DownstreamServiceError(
                code="DOWNSTREAM_INVALID_RESPONSE",
                message=f"转换后的专业服务响应未通过总控契约校验：{exc}",
                retryable=False,
            ) from exc
        return payload

    @abstractmethod
    def build_downstream_payload(
        self, supervisor_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """构造真实专业服务请求。"""

    @abstractmethod
    def normalize_response(
        self,
        supervisor_payload: dict[str, Any],
        downstream_response: dict[str, Any],
        latency_ms: int,
    ) -> dict[str, Any]:
        """把真实服务响应转换回总控统一契约。"""

    def normalize_http_error(
        self,
        supervisor_payload: dict[str, Any],
        response: httpx.Response,
        latency_ms: int,
    ) -> dict[str, Any] | None:
        """允许具体适配器把业务性 HTTP 错误转换为正常业务状态。"""

        return None

    @staticmethod
    def _response_base(
        payload: dict[str, Any],
        *,
        service: str,
        scene: str,
        status: str,
        summary: str,
        latency_ms: int,
        confidence: float | None = None,
        required_information: list[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        version = HttpTaskAgent._result_version(payload)
        return {
            "requestId": payload["requestId"],
            "taskId": payload["taskId"],
            "traceId": payload["traceId"],
            "resultId": f"result_{service}_{payload['taskId']}_v{version}",
            "scene": scene,
            "status": status,
            "version": version,
            "summary": summary,
            "confidence": confidence,
            "requiredInformation": required_information or [],
            "provenance": {
                "serviceVersion": service,
                "modelVersion": "real-service-runtime",
                "promptVersion": "supervisor-adapter-v1",
                "metadataVersion": (
                    payload.get("context", {}).get("metadataVersion") or "runtime"
                ),
            },
            "latencyMs": latency_ms,
            "error": error,
        }

    @staticmethod
    def _result_version(payload: dict[str, Any]) -> int:
        previous_versions = [
            item.get("previousVersion", 0)
            for item in payload.get("correctionContext", [])
            if isinstance(item, dict)
        ]
        return max(previous_versions, default=0) + 1

    @staticmethod
    def _raise_http_error(response: httpx.Response) -> None:
        code = "DOWNSTREAM_UNAVAILABLE"
        message = f"专业服务返回HTTP {response.status_code}"
        retryable = response.status_code in {429, 502, 503, 504}
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                detail = payload.get("detail")
                if isinstance(error, dict):
                    code = str(error.get("code") or code)
                    message = str(error.get("message") or message)
                    retryable = bool(error.get("retryable", retryable))
                elif isinstance(detail, dict):
                    code = str(
                        detail.get("error_code") or detail.get("code") or code
                    )
                    message = str(
                        detail.get("detail") or detail.get("message") or message
                    )
                elif detail:
                    message = str(detail)
                code = str(payload.get("error_code") or code)
        except ValueError:
            pass
        raise DownstreamServiceError(
            code=code,
            message=message,
            retryable=retryable,
            http_status=response.status_code,
        )


class AssetHttpTaskAgent(HttpTaskAgent):
    """把资产 Tool 请求转换为 Task1 自然语言/SQL增强资产检索。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            endpoint="/api/v1/task1/search-by-text",
            request_schema="AssetDuplicateRequest",
            response_schema="AssetServiceResponse",
            timeout_seconds=timeout_seconds,
            token=token,
            transport=transport,
            default_headers=default_headers,
        )

    def build_downstream_payload(
        self, supervisor_payload: dict[str, Any]
    ) -> dict[str, Any]:
        draft = supervisor_payload["draftAsset"]
        query_parts = [
            draft.get("assetName"),
            draft.get("assetDescription"),
            draft.get("metricDefinition"),
        ]
        if draft.get("grain"):
            query_parts.append("粒度：" + "、".join(draft["grain"]))
        query = "；".join(str(item) for item in query_parts if item)
        if not query:
            query = supervisor_payload["originalQuery"]
        request: dict[str, Any] = {
            "query": query,
            "top_k": min(int(supervisor_payload["searchOptions"]["topK"]), 20),
        }
        if draft.get("assetSql"):
            request["sql_text"] = draft["assetSql"]
        return request

    def normalize_response(
        self,
        supervisor_payload: dict[str, Any],
        downstream_response: dict[str, Any],
        latency_ms: int,
    ) -> dict[str, Any]:
        raw_candidates = downstream_response.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise ValueError("candidates 必须是数组")
        requested_min_score = float(supervisor_payload["searchOptions"]["minScore"])
        candidates = []
        for item in raw_candidates:
            score = _score(item.get("recall_score"))
            if score < requested_min_score:
                continue
            components = item.get("score_components") or {}
            semantic = _score(components.get("embedding", components.get("keyword", 0)))
            logic = _score(
                components.get("sql_logic", components.get("sql_identifier", 0))
            )
            candidates.append(
                {
                    "assetId": str(item["asset_id"]),
                    "assetName": str(item["asset_name"]),
                    "businessDomain": item.get("business_domain"),
                    "owner": None,
                    "totalScore": score,
                    "scores": {
                        "semantic": semantic,
                        "logic": logic,
                        "lineage": 0.0,
                    },
                    "evidence": [
                        "Task1返回的是资产检索候选，尚未执行正式三层重复判定。",
                        f"检索模式：{downstream_response.get('retrieval_mode', 'UNKNOWN')}",
                    ],
                    "recommendation": "REVIEW",
                    "reuseAdvice": "请补充完整资产画像后再执行正式三层判重。",
                }
            )

        has_candidates = bool(candidates)
        summary = (
            f"发现{len(candidates)}项达到总控阈值的可复用候选，需人工复核"
            if has_candidates
            else "未发现达到总控阈值的可复用候选"
        )
        response = self._response_base(
            supervisor_payload,
            service="task1-real",
            scene="ASSET_DUPLICATE",
            status="REVIEW_REQUIRED" if has_candidates else "SUCCESS",
            summary=summary,
            latency_ms=latency_ms,
            confidence=max((item["totalScore"] for item in candidates), default=0.0),
        )
        response["result"] = {
            "kind": "duplicate",
            "canReuse": has_candidates,
            "recommendation": "MANUAL_REVIEW" if has_candidates else "CREATE_NEW_ASSET",
            "threshold": requested_min_score,
            "candidates": candidates,
        }
        return response


class SqlHttpTaskAgent(HttpTaskAgent):
    """把 SQL Tool 请求转换为本体治理的 Text-to-SQL 查询。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            endpoint="/api/v1/query",
            request_schema="SqlGenerationRequest",
            response_schema="SqlServiceResponse",
            timeout_seconds=timeout_seconds,
            token=token,
            transport=transport,
            default_headers=default_headers,
        )

    def build_downstream_payload(
        self, supervisor_payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "question": supervisor_payload["originalQuery"],
            "query_mode": "ontology",
            "sql_asset_enabled": True,
        }

    def normalize_response(
        self,
        supervisor_payload: dict[str, Any],
        downstream_response: dict[str, Any],
        latency_ms: int,
    ) -> dict[str, Any]:
        query_status = str(downstream_response.get("status", "failed"))
        if query_status in {"clarification_required", "unsupported"}:
            semantic_query = downstream_response.get("semantic_query") or {}
            return self._clarification_response(
                supervisor_payload,
                semantic_query.get("clarification_question")
                or downstream_response.get("unsupported_reason")
                or "请补充当前已发布本体能够识别的业务指标和查询口径。",
                latency_ms,
            )
        if query_status != "success":
            raise DownstreamServiceError(
                code=str(downstream_response.get("error_code") or "TEXT2SQL_FAILED"),
                message=str(
                    downstream_response.get("unsupported_reason")
                    or downstream_response.get("explanation")
                    or "Text-to-SQL工作流执行失败"
                ),
                retryable=False,
            )

        generated_sql = downstream_response.get("generated_sql")
        if not generated_sql:
            raise ValueError("成功响应缺少 generated_sql")
        confidence = _score(downstream_response.get("confidence"))
        response = self._response_base(
            supervisor_payload,
            service="text2sql-real",
            scene="SQL_GENERATION",
            status="REVIEW_REQUIRED",
            summary="已基于当前发布本体生成并执行只读SQL，等待人工确认",
            latency_ms=latency_ms,
            confidence=confidence,
        )
        response["result"] = {
            "kind": "sql",
            "template": _sql_template(downstream_response),
            "sqlDialect": "POSTGRESQL",
            "sql": str(generated_sql),
            "recommendedTables": [
                {
                    "tableName": str(table),
                    "reason": "由当前已发布业务本体解析并通过策略校验",
                    "confidence": confidence,
                }
                for table in downstream_response.get("selected_tables", [])
            ],
            "recommendedFields": [
                {
                    "tableName": str(table),
                    "fieldName": str(column),
                    "role": "OUTPUT",
                    "reason": "由指标、维度和过滤条件的本体映射解析",
                }
                for table, columns in downstream_response.get(
                    "selected_columns", {}
                ).items()
                for column in columns
            ],
            "explanation": downstream_response.get("explanation"),
            "assumptions": [],
            "validation": _sql_validation(downstream_response),
        }
        return response

    def normalize_http_error(
        self,
        supervisor_payload: dict[str, Any],
        response: httpx.Response,
        latency_ms: int,
    ) -> dict[str, Any] | None:
        if response.status_code not in {400, 422}:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "unsupported":
            return None
        question = payload.get("detail") or "当前已发布本体不支持该查询，请补充业务指标。"
        if isinstance(question, dict):
            question = question.get("detail") or question.get("message") or str(question)
        return self._clarification_response(
            supervisor_payload, str(question), latency_ms
        )

    def _clarification_response(
        self,
        supervisor_payload: dict[str, Any],
        question: str,
        latency_ms: int,
    ) -> dict[str, Any]:
        response = self._response_base(
            supervisor_payload,
            service="text2sql-real",
            scene="SQL_GENERATION",
            status="NEED_MORE_INFORMATION",
            summary=question,
            latency_ms=latency_ms,
            required_information=["requirement.metric"],
        )
        response["result"] = {
            "kind": "clarification",
            "question": question,
            "supportedScenes": ["SQL_GENERATION"],
        }
        return response


def _score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


def _sql_template(response: dict[str, Any]) -> dict[str, Any] | None:
    selected = response.get("selected_sql_asset")
    if not isinstance(selected, dict):
        return None
    template_id = selected.get("id") or selected.get("asset_id")
    if not template_id:
        return None
    return {
        "templateId": str(template_id),
        "templateName": str(
            selected.get("name") or selected.get("question") or template_id
        ),
        "similarity": _score(selected.get("similarity", selected.get("score"))),
        "owner": selected.get("owner"),
    }


def _sql_validation(response: dict[str, Any]) -> dict[str, Any]:
    common = response.get("common_validation_report") or {}
    policy = response.get("ontology_policy_report") or {}
    combined = response.get("validation_report") or {}
    common_valid = bool(common.get("valid", combined.get("valid", False)))
    policy_valid = bool(policy.get("valid", combined.get("valid", False)))
    read_only = bool(common.get("read_only", combined.get("read_only", False)))
    passed = common_valid and policy_valid and read_only
    checks = [
        {
            "type": "SYNTAX",
            "passed": common_valid,
            "message": "SQL语法和物理目录校验通过" if common_valid else "SQL语法或物理目录校验失败",
        },
        {
            "type": "LOGIC",
            "passed": policy_valid,
            "message": "本体策略校验通过" if policy_valid else "本体策略校验失败",
        },
        {
            "type": "SAFETY",
            "passed": read_only,
            "message": "只读安全校验通过" if read_only else "只读安全校验失败",
        },
    ]
    warnings = list(
        dict.fromkeys(
            [
                *combined.get("warnings", []),
                *common.get("warnings", []),
                *policy.get("warnings", []),
                *response.get("validation_errors", []),
            ]
        )
    )
    return {
        "passed": passed,
        "riskLevel": "LOW" if passed and not warnings else ("MEDIUM" if passed else "HIGH"),
        "checks": checks,
        "warnings": [str(item) for item in warnings],
    }
