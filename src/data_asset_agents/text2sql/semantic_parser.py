from __future__ import annotations

import json
import re
from datetime import date

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import Dimension, Metric, OntologyBundle
from data_asset_agents.text2sql.models import (
    SemanticFilter,
    SemanticOrderBy,
    SemanticQuery,
    SemanticQueryDraft,
    TimeRange,
)


class SemanticQueryParser:
    """Parse natural language into business-only structured semantics."""

    def __init__(self, settings: Settings, factory: ModelFactory | None = None) -> None:
        self.settings = settings
        self.factory = factory or ModelFactory(settings)

    @staticmethod
    def _matches(question: str, name: str, synonyms: list[str]) -> bool:
        lowered = question.lower()
        return any(phrase.lower() in lowered for phrase in [name, *synonyms])

    def parse(
        self,
        question: str,
        bundle: OntologyBundle,
        allowed_concept_ids: set[str] | None = None,
    ) -> SemanticQuery:
        draft = (
            self._mock_draft(question, bundle)
            if self.settings.llm_mode == "mock"
            else self._live_draft(question, bundle, allowed_concept_ids)
        )
        return self._normalize(question, draft, bundle)

    def _mock_draft(self, question: str, bundle: OntologyBundle) -> SemanticQueryDraft:
        metrics = [
            metric.id
            for metric in bundle.metrics
            if self._matches(question, metric.name, metric.synonyms)
        ]
        credit_context = any(term in question for term in ("信用卡", "贷记卡", "消费"))
        replacements = {
            "transaction_amount": "credit_card_transaction_amount",
            "transaction_count": "credit_card_transaction_count",
        }
        if credit_context:
            metrics = [replacements.get(metric_id, metric_id) for metric_id in metrics]
        metrics = list(dict.fromkeys(metrics))
        if "credit_card_transaction_amount" in metrics:
            metrics = [item for item in metrics if item != "transaction_amount"]
        if "credit_card_transaction_count" in metrics:
            metrics = [item for item in metrics if item != "transaction_count"]

        dimensions = [
            dimension.id
            for dimension in bundle.dimensions
            if self._matches(question, dimension.name, dimension.synonyms)
        ]
        filters: list[SemanticFilter] = []
        for dimension in bundle.dimensions:
            phrases = sorted([dimension.name, *dimension.synonyms], key=len, reverse=True)
            for phrase in phrases:
                filter_match = re.search(
                    rf"{re.escape(phrase)}\s*(?:为|是|=)\s*"
                    r"([A-Za-z0-9_\u4e00-\u9fff]+)",
                    question,
                    flags=re.IGNORECASE,
                )
                if filter_match:
                    filters.append(
                        SemanticFilter(
                            concept_id=f"dimension:{dimension.id}",
                            value=filter_match.group(1),
                        )
                    )
                    break
        relative = re.search(r"(?:近|最近)\s*(\d+)\s*天", question)
        absolute_dates = re.findall(r"(\d{4}-\d{2}-\d{2})", question)
        if relative:
            time_range = TimeRange(
                kind="relative_days",
                days=int(relative.group(1)),
                original_text=relative.group(0),
            )
        elif absolute_dates:
            time_range = TimeRange(
                kind="absolute",
                start=date.fromisoformat(absolute_dates[0]),
                end=date.fromisoformat(absolute_dates[-1]),
                original_text=" 至 ".join(absolute_dates),
            )
        else:
            time_range = TimeRange()

        top_match = re.search(r"(?:前|top\s*)(\d+)", question, flags=re.IGNORECASE)
        top_n = int(top_match.group(1)) if top_match else None
        order_by = (
            [SemanticOrderBy(target=metrics[0], direction="desc")]
            if top_n and metrics
            else []
        )
        domain_hit = any(
            self._matches(question, item.name, item.synonyms)
            for item in [*bundle.concepts, *bundle.metrics, *bundle.dimensions]
        )
        intent = "detail" if any(term in question for term in ("明细", "详情")) else (
            "aggregate" if metrics else "unknown"
        )
        return SemanticQueryDraft(
            metrics=metrics,
            dimensions=list(dict.fromkeys(dimensions)),
            filters=filters,
            time_range=time_range,
            order_by=order_by,
            limit=top_n,
            top_n=top_n,
            intent=intent,
            confidence=0.98 if metrics else (0.45 if domain_hit else 0.0),
        )

    def _live_draft(
        self,
        question: str,
        bundle: OntologyBundle,
        allowed_concept_ids: set[str] | None = None,
    ) -> SemanticQueryDraft:
        allowed = allowed_concept_ids or {
            item.id for item in [*bundle.metrics, *bundle.dimensions]
        }
        catalog = {
            "metrics": [
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "synonyms": item.synonyms,
                }
                for item in bundle.metrics
                if item.id in allowed
            ],
            "dimensions": [
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "synonyms": item.synonyms,
                }
                for item in bundle.dimensions
                if item.id in allowed
            ],
        }
        prompt = (
            "将问题解析为 SemanticQueryDraft。metrics、dimensions、filters 和 order_by "
            "只能引用目录中的业务语义 ID 或名称，禁止输出数据库、表、字段或 SQL。"
            "不确定时降低 confidence，不要猜测。\n"
            f"业务目录：{json.dumps(catalog, ensure_ascii=False)}\n"
            f"用户问题：{question}"
        )
        structured = self.factory.chat_model().with_structured_output(
            SemanticQueryDraft,
            method="function_calling",
        )
        return SemanticQueryDraft.model_validate(structured.invoke(prompt))

    @staticmethod
    def _resolve_candidates(
        candidates: list[str],
        catalog: list[Metric | Dimension],
    ) -> tuple[list[str], list[str]]:
        resolved: list[str] = []
        unknown: list[str] = []
        for candidate in candidates:
            matches = [
                item
                for item in catalog
                if candidate == item.id
                or candidate == item.name
                or candidate in item.synonyms
            ]
            if len(matches) == 1:
                resolved.append(matches[0].id)
            else:
                unknown.append(candidate)
        return list(dict.fromkeys(resolved)), unknown

    def _normalize(
        self,
        question: str,
        draft: SemanticQueryDraft,
        bundle: OntologyBundle,
    ) -> SemanticQuery:
        metric_ids, unknown_metrics = self._resolve_candidates(
            draft.metrics, list(bundle.metrics)
        )
        dimension_ids, unknown_dimensions = self._resolve_candidates(
            draft.dimensions, list(bundle.dimensions)
        )
        filter_lookup = {
            value: f"dimension:{item.id}"
            for item in bundle.dimensions
            for value in [item.id, item.name, *item.synonyms]
        }
        filter_lookup.update(
            {
                value: item.id
                for item in bundle.concepts
                for value in [item.id, item.name, *item.synonyms]
            }
        )
        normalized_filters = [
            item.model_copy(
                update={
                    "concept_id": filter_lookup.get(item.concept_id, item.concept_id)
                }
            )
            for item in draft.filters
        ]
        known_filter_ids = set(filter_lookup.values())
        unknown_filters = [
            item.concept_id
            for item in normalized_filters
            if item.concept_id not in known_filter_ids
        ]
        forbidden = {
            table.name for table in bundle.tables
        } | {column for table in bundle.tables for column in table.columns}
        business_identifiers = {
            item.id for item in [*bundle.metrics, *bundle.dimensions, *bundle.concepts]
        }
        emitted_identifiers = [
            *draft.metrics,
            *draft.dimensions,
            *(item.concept_id for item in normalized_filters),
            *(item.target for item in draft.order_by),
        ]
        physical_leak = sorted(
            identifier
            for identifier in emitted_identifiers
            if identifier in forbidden and identifier not in business_identifiers
        )
        ambiguous = bool(
            unknown_metrics or unknown_dimensions or unknown_filters or physical_leak
        )
        clarification = ambiguous or (
            not metric_ids and 0 < draft.confidence < 0.65
        )
        metric_names = [
            item.name for item in bundle.metrics if item.id in metric_ids
        ]
        dimension_names = [
            item.name for item in bundle.dimensions if item.id in dimension_ids
        ]
        clarification_question = None
        if clarification:
            unresolved = unknown_metrics + unknown_dimensions + unknown_filters
            clarification_question = (
                "请明确要查询的标准指标或维度"
                + (f"：{', '.join(unresolved)}" if unresolved else "，例如交易金额或交易笔数")
            )
        order_lookup = {
            value: item.id
            for item in [*bundle.metrics, *bundle.dimensions]
            for value in [item.id, item.name, *item.synonyms]
        }
        normalized_order = [
            item.model_copy(update={"target": order_lookup.get(item.target, item.target)})
            for item in draft.order_by
        ]
        unknown_order = [
            item.target
            for item in normalized_order
            if item.target not in {*metric_ids, *dimension_ids}
        ]
        if unknown_order:
            clarification = True
            clarification_question = (
                "请明确排序依据：" + ", ".join(unknown_order)
            )
        return SemanticQuery(
            metric_ids=metric_ids,
            dimension_ids=dimension_ids,
            metric_names=metric_names,
            dimension_names=dimension_names,
            filters=[
                item
                for item in normalized_filters
                if item.concept_id not in unknown_filters
            ],
            time_range=draft.time_range,
            order_by=normalized_order,
            limit=draft.limit,
            top_n=draft.top_n,
            intent=draft.intent,
            confidence=draft.confidence,
            clarification_required=clarification,
            clarification_question=clarification_question,
        )
