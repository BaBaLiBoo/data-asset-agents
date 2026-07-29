from __future__ import annotations

import pytest

from app.evaluation import (
    ScoreRecord,
    ThresholdProfile,
    calibrate_thresholds,
    classification_metrics,
)
from app.models import ComparisonMode, DatasetSplit, GoldLabel


def record(
    pair_id: str,
    score: float,
    label: GoldLabel,
    split: DatasetSplit = DatasetSplit.DEV,
) -> ScoreRecord:
    return ScoreRecord(
        pair_id=pair_id,
        family_id=f"F-{pair_id}",
        split=split,
        scenario="test",
        label=label,
        mode=ComparisonMode.KEYWORD_BASELINE,
        score=score,
        evidence_coverage=1.0,
        semantic_score=None,
        logic_score=None,
        lineage_score=None,
        latency_ms=1.0,
    )


def test_metrics_treat_suspected_as_review_not_auto_correct() -> None:
    records = [
        record("1", 0.95, GoldLabel.DUPLICATE),
        record("2", 0.80, GoldLabel.DUPLICATE),
        record("3", 0.20, GoldLabel.NOT_DUPLICATE),
        record("4", 0.80, GoldLabel.NOT_DUPLICATE),
    ]
    metrics = classification_metrics(
        records,
        ThresholdProfile(suspected=0.75, duplicate=0.90, minimum_evidence_coverage=0.70),
    )
    assert metrics["true_positive"] == 1
    assert metrics["true_negative"] == 1
    assert metrics["accuracy"] == pytest.approx(0.5)
    assert metrics["candidate_recall"] == pytest.approx(1.0)
    assert metrics["review_rate"] == pytest.approx(0.5)


def test_calibration_uses_dev_and_prefers_precision_at_least_90_percent() -> None:
    records = [
        record("d1", 0.96, GoldLabel.DUPLICATE),
        record("d2", 0.93, GoldLabel.DUPLICATE),
        record("d3", 0.80, GoldLabel.NOT_DUPLICATE),
        record("d4", 0.20, GoldLabel.NOT_DUPLICATE),
        record("t1", 0.99, GoldLabel.NOT_DUPLICATE, DatasetSplit.TEST),
    ]
    profile = calibrate_thresholds(records)
    assert profile.duplicate <= 0.93
    dev_metrics = classification_metrics(
        [item for item in records if item.split == DatasetSplit.DEV],
        profile,
    )
    assert dev_metrics["precision"] >= 0.90

