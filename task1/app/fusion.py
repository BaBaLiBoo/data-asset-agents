from __future__ import annotations

from dataclasses import dataclass

from .models import Decision, LayerScore
from .similarity_utils import clamp


@dataclass(frozen=True, slots=True)
class FusionOutcome:
    score: float
    evidence_coverage: float
    decision: Decision
    explanation: list[str]
    warnings: list[str]


class ThreeLayerFusion:
    def __init__(
        self,
        *,
        semantic_weight: float = 0.40,
        logic_weight: float = 0.40,
        lineage_weight: float = 0.20,
        suspected_threshold: float = 0.75,
        duplicate_threshold: float = 0.88,
        minimum_evidence_coverage: float = 0.70,
    ) -> None:
        self.weights = {
            "semantic": semantic_weight,
            "logic": logic_weight,
            "lineage": lineage_weight,
        }
        if abs(sum(self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("three-layer weights must sum to 1")
        if not 0.0 <= suspected_threshold <= duplicate_threshold <= 1.0:
            raise ValueError("thresholds must satisfy 0 <= suspected <= duplicate <= 1")
        self.suspected_threshold = suspected_threshold
        self.duplicate_threshold = duplicate_threshold
        self.minimum_evidence_coverage = minimum_evidence_coverage

    def fuse(
        self,
        semantic: LayerScore,
        logic: LayerScore,
        lineage: LayerScore,
    ) -> FusionOutcome:
        layers = {
            "semantic": semantic,
            "logic": logic,
            "lineage": lineage,
        }
        denominator = sum(
            self.weights[name] * layer.quality
            for name, layer in layers.items()
            if layer.available and layer.score is not None
        )
        if denominator <= 0:
            raise ValueError("no usable evidence for three-layer fusion")
        numerator = sum(
            self.weights[name] * layer.quality * float(layer.score)
            for name, layer in layers.items()
            if layer.available and layer.score is not None
        )
        score = clamp(numerator / denominator)
        coverage = clamp(denominator)
        if score >= self.duplicate_threshold and coverage >= self.minimum_evidence_coverage:
            decision = Decision.DUPLICATE
        elif score >= self.suspected_threshold:
            decision = Decision.SUSPECTED_DUPLICATE
        else:
            decision = Decision.NOT_DUPLICATE
        explanation = [
            f"语义层={semantic.score:.3f}, 质量={semantic.quality:.3f}"
            if semantic.available and semantic.score is not None
            else "语义层不可用",
            f"逻辑层={logic.score:.3f}, 质量={logic.quality:.3f}"
            if logic.available and logic.score is not None
            else "逻辑层不可用",
            f"血缘层={lineage.score:.3f}, 质量={lineage.quality:.3f}"
            if lineage.available and lineage.score is not None
            else "血缘层不可用",
            f"质量加权融合={score:.3f}, 证据覆盖率={coverage:.3f}",
        ]
        warnings = [warning for layer in layers.values() for warning in layer.warnings]
        if score >= self.duplicate_threshold and coverage < self.minimum_evidence_coverage:
            warnings.append("融合分达到重复阈值，但证据覆盖率不足，已降级为疑似重复")
        return FusionOutcome(
            score=score,
            evidence_coverage=coverage,
            decision=decision,
            explanation=explanation,
            warnings=warnings,
        )


def decide_single_score(
    score: float,
    *,
    suspected_threshold: float,
    duplicate_threshold: float,
) -> Decision:
    if score >= duplicate_threshold:
        return Decision.DUPLICATE
    if score >= suspected_threshold:
        return Decision.SUSPECTED_DUPLICATE
    return Decision.NOT_DUPLICATE

