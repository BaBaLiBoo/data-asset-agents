from __future__ import annotations

import pytest

from app.models import AssetInput
from app.sql_logic import SQLLogicComparator
from tests.test_models import sample_asset


BASE_SQL = """
SELECT
  a.customer_id,
  DATE_TRUNC('month', a.balance_date) AS stat_month,
  SUM(a.end_balance) AS total_balance
FROM dwd_account_daily_balance a
WHERE a.currency_code = 'CNY' AND a.customer_type = 'PERSONAL'
GROUP BY a.customer_id, DATE_TRUNC('month', a.balance_date)
"""


def asset(asset_id: str, sql: str) -> AssetInput:
    payload = sample_asset()
    payload.update({"asset_id": asset_id, "sql_text": sql})
    return AssetInput.model_validate(payload)


def test_format_alias_and_and_order_are_logically_equivalent() -> None:
    variant = """
    select x.customer_id as cust, date_trunc('month',x.balance_date) month_id,
           sum(x.end_balance) total
    from dwd_account_daily_balance x
    where x.customer_type='PERSONAL' and x.currency_code='CNY'
    group by x.customer_id,date_trunc('month',x.balance_date)
    """
    result = SQLLogicComparator().compare(asset("A", BASE_SQL), asset("B", variant))
    assert result.available is True
    assert result.score == pytest.approx(1.0)


def test_single_read_only_cte_wrapper_is_equivalent() -> None:
    variant = f"WITH source_asset AS ({BASE_SQL}) SELECT * FROM source_asset"
    result = SQLLogicComparator().compare(asset("A", BASE_SQL), asset("B", variant))
    assert result.score == pytest.approx(1.0)


def test_aggregation_change_reduces_logic_score() -> None:
    variant = BASE_SQL.replace("SUM(a.end_balance)", "AVG(a.end_balance)")
    result = SQLLogicComparator().compare(asset("A", BASE_SQL), asset("B", variant))
    assert result.available is True
    assert result.components["group_agg"] is not None
    assert result.components["group_agg"] < 1.0
    assert result.score is not None and result.score < 1.0


def test_different_literal_same_category_gets_partial_predicate_credit() -> None:
    variant = BASE_SQL.replace("'CNY'", "'USD'")
    result = SQLLogicComparator().compare(asset("A", BASE_SQL), asset("B", variant))
    assert result.available is True
    assert result.components["predicate"] is not None
    assert 0.0 < result.components["predicate"] < 1.0


def test_parse_failure_is_unavailable_not_zero_similarity() -> None:
    result = SQLLogicComparator().compare(
        asset("A", BASE_SQL),
        asset("B", "SELECT FROM WHERE broken"),
    )
    assert result.available is False
    assert result.score is None
    assert result.quality == 0.0
    assert result.warnings


def test_fingerprint_from_sql_extracts_identifiers() -> None:
    comparator = SQLLogicComparator()
    fingerprint = comparator.build_fingerprint_from_sql(
        "SELECT customer_id, AVG(balance) AS avg_balance "
        "FROM dwd_customer_deposit "
        "WHERE currency_code = 'CNY' GROUP BY customer_id",
        "spark",
    )
    assert fingerprint.parse_success is True
    assert fingerprint.input_tables == ("dwd_customer_deposit",)
    assert {
        "customer_id",
        "balance",
        "currency_code",
    }.issubset(set(fingerprint.columns))
    assert comparator.identifier_similarity(fingerprint, fingerprint) == 1.0
