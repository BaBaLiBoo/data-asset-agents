from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedQuery:
    original_query: str
    normalized_query: str
    method: str


class QueryNormalizer:
    """Conservatively removes common intent phrases without inventing business terms."""

    _PREFIX_PATTERNS = (
        r"^\s*(?:请|麻烦)?(?:帮我|帮忙)?(?:检查|查询|查找|看看|看下|确认|判断)\s*",
        r"^\s*(?:我想|我要|需要|准备|计划)(?:开发|新建|创建|建设|做)?\s*",
        r"^\s*(?:想要|希望)(?:开发|新建|创建|建设|做)?\s*",
    )
    _SUFFIX_PATTERNS = (
        r"\s*[，,]?(?:看看|看下)?(?:是否|有没有|有无)(?:已经|已)?(?:存在|建设|开发)(?:过)?(?:相同|相似|重复)?(?:的)?(?:资产|指标|数据)?\s*[？?。！!]*$",
        r"\s*[，,]?(?:看看|看下)?(?:有没有|有无|是否有)(?:已有|现有|存量)?资产(?:可以|可)?(?:复用|使用)\s*[？?。！!]*$",
        r"\s*(?:有没有|是否有)(?:重复|相似|一样)(?:的)?(?:资产|指标|数据)?\s*[？?。！!]*$",
        r"\s*(?:是否|能否|可否)(?:复用|直接使用)(?:已有|现有|存量)?资产\s*[？?。！!]*$",
        r"\s*(?:帮我)?(?:查重|去重|判重|检查重复)\s*[？?。！!]*$",
    )
    _FILLER_PATTERNS = (
        r"\b(?:这个|一下|一下子)\b",
        r"(?:已经|已有|现有|存量)的?(?:资产|指标)",
    )
    _QUOTE_PATTERNS = (
        r"[“\"]([^”\"]+)[”\"]",
        r"[‘']([^’']+)[’']",
    )

    def normalize(self, query: str) -> NormalizedQuery:
        original = query.strip()
        normalized = re.sub(r"\s+", " ", original)

        for pattern in self._QUOTE_PATTERNS:
            match = re.search(pattern, normalized)
            if match:
                quoted = self._clean_candidate(match.group(1))
                if self._is_usable(quoted):
                    return NormalizedQuery(
                        original_query=original,
                        normalized_query=quoted,
                        method="QUOTE_EXTRACTED",
                    )

        for pattern in self._PREFIX_PATTERNS:
            normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)
        for pattern in self._SUFFIX_PATTERNS:
            normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)
        for pattern in self._FILLER_PATTERNS:
            normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)

        normalized = self._clean_candidate(normalized)

        if not self._is_usable(normalized) or normalized == original:
            return NormalizedQuery(
                original_query=original,
                normalized_query=original,
                method="ORIGINAL_FALLBACK",
            )
        return NormalizedQuery(
            original_query=original,
            normalized_query=normalized,
            method="RULE_MATCHED",
        )

    @staticmethod
    def _clean_candidate(value: str) -> str:
        value = re.sub(
            r"^[，,：:；;\s]+|[，,：:；;\s？?。！!]+$",
            "",
            value,
        )
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _is_usable(value: str) -> bool:
        compact = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
        return len(compact) >= 2
