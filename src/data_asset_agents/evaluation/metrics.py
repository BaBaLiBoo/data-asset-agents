from __future__ import annotations

from collections import Counter
from statistics import mean

import sqlglot
from sqlglot import exp

from data_asset_agents.evaluation.models import (
    BenchmarkCase,
    EvaluationCaseResult,
    EvaluationMetrics,
)


def normalize_join(condition: str) -> frozenset[tuple[str, str]]:
    expression = sqlglot.parse_one(f"SELECT 1 FROM a JOIN b ON {condition}", read="postgres")
    equality = next(expression.find_all(exp.EQ), None)
    if (
        equality is None
        or not isinstance(equality.left, exp.Column)
        or not isinstance(equality.right, exp.Column)
    ):
        return frozenset()
    return frozenset(
        (
            (equality.left.table, equality.left.name),
            (equality.right.table, equality.right.name),
        )
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * fraction), len(ordered) - 1)
    return ordered[index]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _mean_recall(expected: list[str], retrieved: list[str]) -> float:
    gold = set(expected)
    return len(gold & set(retrieved)) / len(gold) if gold else 1.0


def _canonical_dicts(items: list[dict[str, object]]) -> set[tuple[tuple[str, str], ...]]:
    return {
        tuple(sorted((str(key), str(value)) for key, value in item.items()))
        for item in items
    }


def _semantic_matches(case: BenchmarkCase, result: EvaluationCaseResult) -> bool:
    output = result.semantic_output
    if output is None:
        return False
    actual_time = dict(output.get("time_range") or {"kind": "none"})
    actual_time.pop("original_text", None)
    actual_time = {key: value for key, value in actual_time.items() if value is not None}
    expected_time = {
        key: value for key, value in case.gold_time_range.items() if value is not None
    }
    return (
        set(output.get("metric_ids", [])) == set(case.gold_metric_ids)
        and set(output.get("dimension_ids", [])) == set(case.gold_dimension_ids)
        and _canonical_dicts(output.get("filters", []))
        == _canonical_dicts(case.gold_semantic_filters)
        and actual_time == expected_time
    )


def calculate_metrics(
    run_id: str,
    cases: list[BenchmarkCase],
    results: list[EvaluationCaseResult],
    *,
    strategy_variant: str,
) -> EvaluationMetrics:
    by_id = {result.case_id: result for result in results}
    paired = [(case, by_id[case.id]) for case in cases if case.id in by_id]
    success_cases = [(case, result) for case, result in paired if case.expected_status == "success"]
    status_accuracy = _ratio(
        sum(case.expected_status == result.predicted_status for case, result in paired),
        len(paired),
    )
    table_exact = _ratio(
        sum(
            set(result.referenced_tables) == set(case.gold_tables) for case, result in success_cases
        ),
        len(success_cases),
    )
    column_exact = _ratio(
        sum(
            set(result.referenced_columns) == set(case.gold_columns)
            for case, result in success_cases
        ),
        len(success_cases),
    )
    table_recall = None
    column_recall = None
    if strategy_variant == "rag":
        table_recall = (
            round(
                mean(
                    _mean_recall(case.gold_tables, result.retrieved_tables)
                    for case, result in success_cases
                ),
                6,
            )
            if success_cases
            else 0.0
        )
        column_recall = round(
            mean(
                _mean_recall(case.gold_columns, result.retrieved_columns)
                for case, result in success_cases
            ),
            6,
        ) if success_cases else 0.0
    join_cases = [(case, result) for case, result in success_cases if case.gold_joins]
    join_exact = (
        _ratio(
            sum(
                {normalize_join(item) for item in result.discovered_joins}
                == {normalize_join(item) for item in case.gold_joins}
                for case, result in join_cases
            ),
            len(join_cases),
        )
        if join_cases
        else None
    )
    lifecycle = [(case, result) for case, result in paired if "lifecycle_distractor" in case.tags]
    lifecycle_rate = (
        _ratio(
            sum(
                any(
                    token in " ".join(result.evaluation_policy_violations).lower()
                    for token in ("deprecated", "temporary", "test")
                )
                for _, result in lifecycle
            ),
            len(lifecycle),
        )
        if lifecycle
        else None
    )
    policy = [(case, result) for case, result in paired if "business_policy" in case.tags]
    policy_accuracy = (
        _ratio(
            sum(not result.evaluation_policy_violations for _, result in policy),
            len(policy),
        )
        if policy
        else None
    )
    parse_rate = _ratio(
        sum(
            result.generated_sql is not None
            and not any("parse" in error.lower() for error in result.common_validation_errors)
            for _, result in success_cases
        ),
        len(success_cases),
    )
    execution_rate = _ratio(
        sum(result.execution_result_hash is not None for _, result in success_cases),
        len(success_cases),
    )
    hash_cases = [
        (case, result) for case, result in paired if case.expected_result_hash is not None
    ]
    result_accuracy = (
        _ratio(
            sum(
                case.expected_result_hash == result.execution_result_hash
                for case, result in hash_cases
            ),
            len(hash_cases),
        )
        if hash_cases
        else None
    )
    semantic_accuracy = None
    if strategy_variant.startswith("ontology"):
        semantic_cases = [(case, result) for case, result in paired if case.gold_metric_ids]
        semantic_accuracy = _ratio(
            sum(_semantic_matches(case, result) for case, result in semantic_cases),
            len(semantic_cases),
        )
    adoption = None
    if strategy_variant == "ontology_full":
        compatible = [result for _, result in paired if result.template_compatible]
        adoption = (
            _ratio(sum(bool(result.template_adopted) for result in compatible), len(compatible))
            if compatible
            else None
        )
    latencies = [result.latency_ms for _, result in paired]
    failures = Counter(result.failure_category for _, result in paired if result.failure_category)
    return EvaluationMetrics(
        run_id=run_id,
        case_count=len(paired),
        status_accuracy=status_accuracy,
        semantic_query_accuracy=semantic_accuracy,
        table_recall_at_k=table_recall,
        table_exact_match=table_exact,
        column_recall_at_k=column_recall,
        column_exact_match=column_exact,
        join_exact_match=join_exact,
        deprecated_table_false_selection_rate=lifecycle_rate,
        business_policy_accuracy=policy_accuracy,
        sql_parse_rate=parse_rate,
        sql_execution_rate=execution_rate,
        result_accuracy=result_accuracy,
        template_adoption_rate=adoption,
        average_latency_ms=round(mean(latencies), 2) if latencies else 0.0,
        p50_latency_ms=round(_percentile(latencies, 0.5), 2),
        p95_latency_ms=round(_percentile(latencies, 0.95), 2),
        failure_distribution=dict(failures),
    )
