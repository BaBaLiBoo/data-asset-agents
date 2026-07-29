from __future__ import annotations

import pytest

from app.lineage import LineageComparator, lineage_quality
from app.models import AssetInput, LineageInput
from tests.test_models import sample_asset


def asset(asset_id: str, lineage: dict) -> AssetInput:
    payload = sample_asset()
    payload.update({"asset_id": asset_id, "lineage": lineage})
    return AssetInput.model_validate(payload)


COMPLETE = {
    "direct_upstreams": ["dwd_account_daily_balance"],
    "root_sources": ["m_savings_account", "m_savings_account_transaction"],
    "column_signatures": ["avg_balance<-dwd_account_daily_balance.end_balance"],
    "coverage": 1.0,
    "freshness": 1.0,
    "source_reliability": 1.0,
}


def test_lineage_quality_formula() -> None:
    lineage = LineageInput(coverage=0.8, freshness=0.5, source_reliability=0.9)
    assert lineage_quality(lineage) == pytest.approx(0.73)


def test_identical_complete_lineage_scores_one() -> None:
    result = LineageComparator().compare(asset("A", COMPLETE), asset("B", COMPLETE))
    assert result.available is True
    assert result.score == pytest.approx(1.0)
    assert result.quality == pytest.approx(1.0)


def test_missing_column_lineage_is_renormalized_and_lowers_quality() -> None:
    missing = {**COMPLETE, "column_signatures": [], "coverage": 0.7}
    result = LineageComparator().compare(asset("A", missing), asset("B", missing))
    assert result.score == pytest.approx(1.0)
    assert result.quality < 1.0
    assert any("字段血缘缺失" in warning for warning in result.warnings)


def test_different_roots_reduce_lineage_score() -> None:
    loan = {
        **COMPLETE,
        "direct_upstreams": ["dwd_loan_account"],
        "root_sources": ["m_loan", "m_loan_transaction"],
        "column_signatures": ["loan_balance<-m_loan.principal_amount"],
    }
    result = LineageComparator().compare(asset("A", COMPLETE), asset("B", loan))
    assert result.score == pytest.approx(0.0)


def test_empty_lineage_is_unavailable_not_zero() -> None:
    empty = {
        "direct_upstreams": [],
        "root_sources": [],
        "column_signatures": [],
        "coverage": 0.0,
        "freshness": 0.0,
        "source_reliability": 0.0,
    }
    result = LineageComparator().compare(asset("A", empty), asset("B", empty))
    assert result.available is False
    assert result.score is None

