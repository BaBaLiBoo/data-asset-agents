from __future__ import annotations

from .models import AssetInput, LayerScore, LineageInput
from .similarity_utils import jaccard, weighted_available


LINEAGE_WEIGHTS = {
    "direct": 0.30,
    "root": 0.50,
    "column": 0.20,
}


def _normalize(values: list[str]) -> list[str]:
    return [" ".join(value.lower().split()) for value in values if value.strip()]


def lineage_quality(lineage: LineageInput) -> float:
    return (
        0.50 * lineage.coverage
        + 0.30 * lineage.freshness
        + 0.20 * lineage.source_reliability
    )


class LineageComparator:
    def compare(self, left: AssetInput, right: AssetInput) -> LayerScore:
        a = left.lineage
        b = right.lineage
        components = {
            "direct": jaccard(
                _normalize(a.direct_upstreams),
                _normalize(b.direct_upstreams),
            ),
            "root": jaccard(
                _normalize(a.root_sources),
                _normalize(b.root_sources),
            ),
            "column": jaccard(
                _normalize(a.column_signatures),
                _normalize(b.column_signatures),
            ),
        }
        score, component_coverage = weighted_available(components, LINEAGE_WEIGHTS)
        if score is None:
            return LayerScore(
                available=False,
                quality=0.0,
                components=components,
                warnings=["两侧均无可比较血缘信息"],
            )
        pair_source_quality = min(lineage_quality(a), lineage_quality(b))
        quality = pair_source_quality * component_coverage
        warnings: list[str] = []
        if components["column"] is None:
            warnings.append("字段血缘缺失，已按可用子项重新归一化")
        if pair_source_quality < 0.70:
            warnings.append(f"血缘来源质量较低：{pair_source_quality:.3f}")
        evidence = [
            f"直接上游相似度={components['direct']:.3f}"
            if components["direct"] is not None
            else "直接上游不可比较",
            f"根源表相似度={components['root']:.3f}"
            if components["root"] is not None
            else "根源表不可比较",
            f"字段血缘相似度={components['column']:.3f}"
            if components["column"] is not None
            else "字段血缘不可比较",
        ]
        return LayerScore(
            score=score,
            quality=quality,
            available=True,
            components=components,
            evidence=evidence,
            warnings=warnings,
        )

