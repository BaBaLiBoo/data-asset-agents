from __future__ import annotations

import pytest

from app.query_normalizer import QueryNormalizer


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我想开发个人客户月均存款", "个人客户月均存款"),
        ("帮我检查个人客户月均存款是否已经存在", "个人客户月均存款"),
        (
            "我想开发个人客户月均存款，看看有没有已有资产可以复用",
            "个人客户月均存款",
        ),
        ("请帮我看看“自然日日均余额”有没有相关资产", "自然日日均余额"),
        ('查找"零售客户月末余额"相关内容', "零售客户月末余额"),
    ],
)
def test_normalizer_extracts_business_phrase(
    query: str,
    expected: str,
) -> None:
    result = QueryNormalizer().normalize(query)
    assert result.normalized_query == expected
    assert result.method != "ORIGINAL_FALLBACK"


def test_unmatched_query_falls_back_to_original() -> None:
    query = "个人客户月均存款"
    result = QueryNormalizer().normalize(query)
    assert result.normalized_query == query
    assert result.method == "ORIGINAL_FALLBACK"


def test_empty_or_too_short_cleaning_falls_back_to_original() -> None:
    for query in ("帮我检查", "我想开发A"):
        result = QueryNormalizer().normalize(query)
        assert result.normalized_query == query
        assert result.method == "ORIGINAL_FALLBACK"


def test_business_terms_are_not_removed() -> None:
    result = QueryNormalizer().normalize(
        "帮我检查个人客户人民币自然日月均余额有没有重复资产"
    )
    for term in ("个人客户", "人民币", "自然日", "月均", "余额"):
        assert term in result.normalized_query
