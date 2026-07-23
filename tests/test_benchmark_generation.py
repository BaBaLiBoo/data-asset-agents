import json
from pathlib import Path

from scripts.generate_benchmark import REFERENCE_DATE, build_cases

ROOT = Path(__file__).parents[1]


def test_reviewed_benchmark_and_hash_contracts_are_frozen() -> None:
    benchmark = json.loads(
        (ROOT / "data/benchmark/text2sql_v1.json").read_text(encoding="utf-8")
    )
    hashes = json.loads(
        (ROOT / "data/benchmark/text2sql_v1_hashes.json").read_text(encoding="utf-8")
    )
    generated = build_cases()
    successful = {
        case["id"] for case in generated if case["expected_status"] == "success"
    }

    assert benchmark["cases"] == generated
    assert len(generated) == 80
    assert len({case["id"] for case in generated}) == 80
    assert set(hashes) == successful
    assert all(value != "0" * 64 for value in hashes.values())
    assert all(
        case["gold_sql"] is None
        for case in generated
        if case["expected_status"] != "success"
    )


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
