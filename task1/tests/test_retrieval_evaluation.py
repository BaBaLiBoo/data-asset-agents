from __future__ import annotations

from scripts.evaluate_retrieval import (
    CandidateFeatures,
    QueryFeatures,
    calculate_metrics,
)
from scripts.generate_retrieval_dataset import generate, validate


def test_generated_retrieval_dataset_has_no_family_leakage() -> None:
    rows = generate()
    validate(rows)
    dev = {
        row["family_id"]
        for row in rows
        if row["split"] == "DEV" and row["has_match"]
    }
    test = {
        row["family_id"]
        for row in rows
        if row["split"] == "TEST" and row["has_match"]
    }
    assert dev
    assert test
    assert dev.isdisjoint(test)


def test_generated_dataset_covers_required_query_types() -> None:
    rows = generate()
    query_types = {row["query_type"] for row in rows}
    assert {
        "STANDARD",
        "SYNONYM",
        "COLLOQUIAL",
        "NEAR_DISTRACTOR",
        "AMBIGUOUS",
        "TEXT_SQL",
        "NO_MATCH",
    }.issubset(query_types)
    assert all(
        len(row["expected_asset_ids"]) >= 2
        for row in rows
        if row["has_match"]
    )
    assert any(row["sql_text"] for row in rows)
    assert any(
        row["sql_text"] == "SELECT FROM WHERE"
        for row in rows
    )


def test_retrieval_metrics_separate_match_and_no_match() -> None:
    matched = QueryFeatures(
        payload={
            "query_id": "Q1",
            "query_type": "STANDARD",
            "has_match": True,
            "expected_asset_ids": ["A1"],
        },
        candidates=(
            CandidateFeatures("A1", embedding=0.9, keyword=0.5),
            CandidateFeatures("A2", embedding=0.2, keyword=0.8),
        ),
        sql_parse_failed=False,
    )
    no_match = QueryFeatures(
        payload={
            "query_id": "Q2",
            "query_type": "NO_MATCH",
            "has_match": False,
            "expected_asset_ids": [],
        },
        candidates=(
            CandidateFeatures("A2", embedding=0.2, keyword=0.1),
        ),
        sql_parse_failed=False,
    )
    metrics = calculate_metrics(
        [matched, no_match],
        weights={"embedding": 1.0, "keyword": 0.0},
        threshold=0.5,
    )
    assert metrics["recall_at_1"] == 1.0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["no_match_accuracy"] == 1.0
    assert metrics["error_recommendation_rate"] == 0.0
