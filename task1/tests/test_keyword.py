from __future__ import annotations

import pytest

from app.keyword import KeywordComparator, jaccard, normalize_text, tokenize
from app.models import AssetInput
from tests.test_models import sample_asset


def asset(**updates) -> AssetInput:
    payload = sample_asset()
    payload.update(updates)
    return AssetInput.model_validate(payload)


def test_normalization_splits_identifiers_without_synonym_mapping() -> None:
    assert normalize_text("CustomerMonthly-Balance") == "customer monthly balance"
    assert "customer" in tokenize("customer_id")
    assert tokenize("个人客户") != tokenize("自然人客户")


def test_jaccard_is_exact_and_symmetric() -> None:
    assert jaccard({"客户", "余额"}, {"余额", "月份"}) == pytest.approx(1 / 3)
    assert jaccard({"余额"}, {"余额"}) == 1.0
    assert jaccard(set(), set()) is None


def test_identical_assets_have_keyword_score_one() -> None:
    item = asset()
    result = KeywordComparator().compare(item, item)
    assert result.available is True
    assert result.score == pytest.approx(1.0)
    assert result.quality == pytest.approx(1.0)


def test_keyword_baseline_uses_only_literal_overlap() -> None:
    left = asset(
        asset_name="个人客户月日均存款",
        description="个人客户人民币存款日均余额",
    )
    right = asset(
        asset_id="A002",
        asset_name="自然人月平均储蓄余额",
        description="自然人每月储蓄平均值",
        business_domain="savings",
        declared_grain=["natural_person", "calendar_month"],
        tables=[{"name": "account_snapshot", "columns": [{"name": "client_no"}]}],
    )
    result = KeywordComparator().compare(left, right)
    assert result.available is True
    assert 0.0 <= result.score < 0.5

