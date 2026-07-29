from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import jieba

from .models import AssetInput, LayerScore
from .similarity_utils import jaccard, weighted_available


jieba.setLogLevel(logging.WARNING)

STOP_WORDS = {
    "的",
    "和",
    "与",
    "及",
    "按",
    "进行",
    "统计",
    "数据",
    "表",
    "资产",
    "the",
    "a",
    "an",
    "of",
    "and",
}

COMPONENT_WEIGHTS = {
    "name": 0.35,
    "description": 0.25,
    "field": 0.25,
    "table": 0.15,
}


def _split_camel(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = _split_camel(value)
    value = value.lower().replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def tokenize(value: str | None) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    tokens: set[str] = set()
    for segment in normalized.split():
        if re.fullmatch(r"[a-z0-9]+", segment):
            candidates = [segment]
        else:
            candidates = [item.strip() for item in jieba.cut(segment, cut_all=False)]
        for token in candidates:
            if len(token) > 1 and token not in STOP_WORDS:
                tokens.add(token)
    return tokens


@dataclass(frozen=True, slots=True)
class KeywordProfile:
    name: set[str]
    description: set[str]
    field: set[str]
    table: set[str]


class KeywordComparator:
    def build_profile(self, asset: AssetInput) -> KeywordProfile:
        description = " ".join(
            item
            for item in (
                asset.description,
                asset.metric_name,
                asset.metric_definition,
                asset.business_domain,
                " ".join(asset.declared_grain),
            )
            if item
        )
        field_text: list[str] = []
        table_text: list[str] = []
        for table in asset.tables:
            table_text.extend(item for item in (table.name, table.cn_name, table.comment) if item)
            for column in table.columns:
                field_text.extend(
                    item
                    for item in (
                        column.name,
                        column.cn_name,
                        column.comment,
                    )
                    if item
                )
        return KeywordProfile(
            name=tokenize(asset.asset_name),
            description=tokenize(description),
            field=tokenize(" ".join(field_text)),
            table=tokenize(" ".join(table_text)),
        )

    def compare_query_to_asset(
        self,
        query_text: str,
        asset: AssetInput,
    ) -> float:
        query_tokens = tokenize(query_text)
        profile = self.build_profile(asset)
        asset_tokens = (
            profile.name
            | profile.description
            | profile.field
            | profile.table
        )
        value = jaccard(query_tokens, asset_tokens)
        return float(value or 0.0)

    def query_score(self, query: str, asset: AssetInput) -> float:
        return self.compare_query_to_asset(query, asset)

    def compare(self, left: AssetInput, right: AssetInput) -> LayerScore:
        a = self.build_profile(left)
        b = self.build_profile(right)
        components = {
            "name": jaccard(a.name, b.name),
            "description": jaccard(a.description, b.description),
            "field": jaccard(a.field, b.field),
            "table": jaccard(a.table, b.table),
        }
        score, quality = weighted_available(components, COMPONENT_WEIGHTS)
        if score is None:
            return LayerScore(
                available=False,
                quality=0.0,
                components=components,
                warnings=["关键词字段全部缺失"],
            )
        strongest = max(
            ((key, value) for key, value in components.items() if value is not None),
            key=lambda item: item[1],
        )
        return LayerScore(
            score=score,
            quality=quality,
            available=True,
            components=components,
            evidence=[f"最高关键词重合项：{strongest[0]}={strongest[1]:.3f}"],
        )
