"""专业 Tool 调用前的结构化参数补全与业务完整性检查。"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..models.api import ChatRequest
from ..models.task_requests import (
    AssetDuplicateToolInput,
    LineageToolInput,
    SqlGenerationToolInput,
)
from ..tools.task_tools import TaskTool, ToolInputError


@dataclass(frozen=True, slots=True)
class RequirementAssessment:
    """总控在执行专业 Tool 前得到的确定性检查结论。"""

    arguments: dict[str, Any]
    missing_fields: tuple[str, ...] = ()
    question: str = ""

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields


class RequirementValidator:
    """校验 JSON Schema 之外的最低业务信息，并合并可信会话上下文。"""

    _INTENT_WORDS = (
        "帮我",
        "请",
        "能否",
        "是否",
        "检查",
        "判断",
        "生成",
        "写一个",
        "写",
        "sql",
        "资产",
        "查重",
        "重复",
        "这个",
        "这张",
        "一下",
    )

    def assess(
        self,
        tool: TaskTool,
        arguments: dict[str, Any],
        request: ChatRequest,
    ) -> RequirementAssessment:
        enriched = self._merge_trusted_context(tool.name, arguments, request)
        try:
            tool_input = tool.input_model.model_validate(enriched)
        except ValidationError as exc:
            raise ToolInputError(str(exc)) from exc

        normalized = tool_input.model_dump(mode="json", by_alias=True)
        if isinstance(tool_input, AssetDuplicateToolInput):
            return self._assess_asset(tool_input, normalized)
        if isinstance(tool_input, SqlGenerationToolInput):
            return self._assess_sql(tool_input, normalized)
        if isinstance(tool_input, LineageToolInput):
            return self._assess_lineage(tool_input, normalized)
        return RequirementAssessment(arguments=normalized)

    @staticmethod
    def _merge_trusted_context(
        tool_name: str,
        arguments: dict[str, Any],
        request: ChatRequest,
    ) -> dict[str, Any]:
        enriched = deepcopy(arguments)
        if tool_name == "generate_sql":
            execution_context = enriched.setdefault("executionContext", {})
            if not isinstance(execution_context, dict):
                return enriched
            if not execution_context.get("sqlDialect") and request.context.sql_dialect:
                execution_context["sqlDialect"] = request.context.sql_dialect
            if not execution_context.get("schemaScope") and request.context.schema_scope:
                execution_context["schemaScope"] = request.context.schema_scope
            return enriched
        if tool_name == "parse_regulatory_lineage":
            if not enriched.get("scriptId") and request.context.script_id:
                enriched["scriptId"] = request.context.script_id
            if not enriched.get("targetField") and request.context.target_field:
                enriched["targetField"] = request.context.target_field
            if not enriched.get("sqlDialect") and request.context.sql_dialect:
                enriched["sqlDialect"] = request.context.sql_dialect
            return enriched
        return enriched

    def _assess_asset(
        self,
        tool_input: AssetDuplicateToolInput,
        normalized: dict[str, Any],
    ) -> RequirementAssessment:
        asset = tool_input.draft_asset
        has_description = any(
            self._is_meaningful(value)
            for value in (
                asset.asset_name,
                asset.asset_description,
                asset.metric_definition,
            )
        )
        has_structured_detail = bool(
            asset.asset_sql
            or asset.fields
            or asset.source_tables
            or asset.upstream_asset_ids
        )
        if has_description or has_structured_detail:
            return RequirementAssessment(arguments=normalized)
        return RequirementAssessment(
            arguments=normalized,
            missing_fields=("draftAsset.assetDescription",),
            question=(
                "请补充待检查资产的名称、业务口径或用途描述；"
                "如果已有加工 SQL、来源表或字段清单，也可以一并提供。"
            ),
        )

    def _assess_sql(
        self,
        tool_input: SqlGenerationToolInput,
        normalized: dict[str, Any],
    ) -> RequirementAssessment:
        requirement = tool_input.requirement
        has_target = any(
            self._is_meaningful(value)
            for value in [*requirement.metric, *requirement.target_fields]
        )
        if has_target:
            return RequirementAssessment(arguments=normalized)
        return RequirementAssessment(
            arguments=normalized,
            missing_fields=("requirement.metric",),
            question=(
                "请补充需要计算的指标或目标字段，例如“新增客户数”；"
                "最好同时说明维度、时间范围和过滤条件。"
            ),
        )

    def _assess_lineage(
        self,
        tool_input: LineageToolInput,
        normalized: dict[str, Any],
    ) -> RequirementAssessment:
        missing: list[str] = []
        if not tool_input.script_id or not tool_input.script_id.strip():
            missing.append("scriptId")
        if not tool_input.target_field or not tool_input.target_field.strip():
            missing.append("targetField")
        if missing:
            return RequirementAssessment(
                arguments=normalized,
                missing_fields=tuple(missing),
                question=(
                    "请先上传监管报送脚本并指定需要解析血缘的目标字段，"
                    "例如 EAST.BD_ODS_BNWYWDBHTB::DBHTH。"
                ),
            )
        return RequirementAssessment(arguments=normalized)

    def _is_meaningful(self, value: str | None) -> bool:
        if not value or not value.strip():
            return False
        normalized = re.sub(r"[\W_]+", "", value.lower())
        for word in self._INTENT_WORDS:
            normalized = normalized.replace(word, "")
        return len(normalized) >= 2
