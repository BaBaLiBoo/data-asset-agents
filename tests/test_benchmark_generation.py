from scripts.generate_benchmark import REFERENCE_DATE, build_cases


def test_benchmark_synonyms_have_question_aligned_gold_contracts() -> None:
    cases = {case["id"]: case for case in build_cases()}

    assert cases["basic-10"]["gold_metric_ids"] == ["average_transaction_amount"]
    assert cases["synonym-03"]["gold_metric_ids"] == ["transaction_count"]
    assert cases["synonym-03"]["gold_dimension_ids"] == ["transaction_channel"]
    assert cases["synonym-04"]["gold_metric_ids"] == ["average_transaction_amount"]
    assert cases["synonym-06"]["gold_metric_ids"] == ["transaction_amount"]
    assert cases["synonym-06"]["gold_time_range"] == {
        "kind": "relative_days",
        "days": 30,
    }
    assert f"DATE '{REFERENCE_DATE}'" in str(cases["synonym-06"]["gold_sql"])
    assert cases["synonym-07"]["gold_metric_ids"] == [
        "credit_card_transaction_count"
    ]
    assert cases["synonym-08"]["gold_metric_ids"] == ["transaction_amount"]


def test_relative_gold_sql_uses_frozen_reference_date() -> None:
    cases = {case["id"]: case for case in build_cases()}

    for case_id in ("filter-01", "filter-02", "filter-03", "filter-04"):
        sql = str(cases[case_id]["gold_sql"])
        assert "CURRENT_DATE" not in sql
        assert f"DATE '{REFERENCE_DATE}'" in sql
        assert cases[case_id]["gold_time_range"]["kind"] == "relative_days"
