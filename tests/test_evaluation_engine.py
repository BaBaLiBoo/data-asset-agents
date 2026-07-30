from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.evaluation.benchmark import load_benchmark
from data_asset_agents.evaluation.metrics import calculate_metrics, normalize_join
from data_asset_agents.evaluation.models import (
    BenchmarkCase,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationRunRequest,
    StrategyResult,
)
from data_asset_agents.evaluation.normalizer import ResultNormalizer
from data_asset_agents.evaluation.repository import MemoryEvaluationRepository
from data_asset_agents.evaluation.service import EvaluationService
from data_asset_agents.evaluation.strategies import StrategyRouter
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.models import ExecutionResult
from data_asset_agents.validation import EvaluationPolicyInspector
from scripts.run_text2sql_reviewed_ontology_v2 import (
    GROUPS,
    _atomic_write_json,
    _case_outcome_comparison,
    _load_progress,
    _result_delta,
    _validate_group_row,
)


def test_benchmark_contract_and_80_case_distribution() -> None:
    suite = load_benchmark("data/benchmark/text2sql_v1.json")
    assert len(suite.cases) == 80
    assert len({case.id for case in suite.cases}) == 80
    assert sum(case.expected_status == "success" for case in suite.cases) == 74
    with pytest.raises(ValidationError):
        BenchmarkCase(
            id="invalid-success",
            question="查询交易金额",
            category="test",
            difficulty="easy",
            expected_status="success",
            gold_tables=["dwd_card_transaction"],
            expected_result_hash="0" * 64,
        )


def test_result_normalizer_stable_types_order_and_hash() -> None:
    normalizer = ResultNormalizer()
    first = ExecutionResult(
        columns=["Amount", "Date", "Flag"],
        rows=[
            {"Amount": Decimal("10.500"), "Date": date(2026, 7, 1), "Flag": True},
            {"Amount": None, "Date": date(2026, 7, 2), "Flag": False},
        ],
        row_count=2,
    )
    reversed_result = first.model_copy(update={"rows": list(reversed(first.rows))})
    assert normalizer.hash(first, order_sensitive=False) == normalizer.hash(
        reversed_result, order_sensitive=False
    )
    assert normalizer.hash(first, order_sensitive=True) != normalizer.hash(
        reversed_result, order_sensitive=True
    )
    payload = normalizer.normalize(first, order_sensitive=True)
    assert payload["columns"] == ["amount", "date", "flag"]
    assert payload["rows"][0] == ["10.5", "2026-07-01", True]
    assert payload["rows"][1][0] is None


def test_join_normalization_is_direction_independent() -> None:
    forward = normalize_join("a.id = b.a_id")
    reverse = normalize_join("b.a_id = a.id")
    assert forward == reverse


def test_metrics_preserve_null_for_not_applicable_and_zero_for_failure() -> None:
    case = BenchmarkCase(
        id="metric-case",
        question="查询交易金额",
        category="test",
        difficulty="easy",
        expected_status="success",
        gold_tables=["dwd_card_transaction"],
        gold_columns=["dwd_card_transaction.txn_amount_cny"],
        gold_sql="SELECT txn_amount_cny FROM dwd_card_transaction",
        expected_result_hash="a" * 64,
    )
    result = EvaluationCaseResult(
        run_id="run",
        case_id=case.id,
        predicted_status="failed",
        referenced_tables=[],
        referenced_columns=[],
        success=False,
        failure_category="execution_error",
    )
    metrics = calculate_metrics("run", [case], [result], strategy_variant="schema")
    assert metrics.table_exact_match == 0
    assert metrics.column_recall_at_k is None
    assert metrics.semantic_query_accuracy is None
    assert metrics.result_accuracy == 0


def test_rag_recall_at_k_uses_retrieved_physical_documents() -> None:
    case = BenchmarkCase(
        id="rag-recall",
        question="查询各分行交易金额",
        category="test",
        difficulty="easy",
        expected_status="success",
        gold_tables=["dwd_card_transaction", "dim_branch"],
        gold_columns=[
            "dwd_card_transaction.txn_amount_cny",
            "dim_branch.branch_name",
        ],
        gold_sql=(
            "SELECT b.branch_name, SUM(t.txn_amount_cny) "
            "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
            "GROUP BY b.branch_name"
        ),
        expected_result_hash="a" * 64,
    )
    result = EvaluationCaseResult(
        run_id="run",
        case_id=case.id,
        predicted_status="success",
        retrieved_tables=["dim_branch", "dwd_card_transaction"],
        retrieved_columns=["dim_branch.branch_name"],
        success=False,
    )
    metrics = calculate_metrics("run", [case], [result], strategy_variant="rag")
    assert metrics.table_recall_at_k == 1
    assert metrics.column_recall_at_k == 0.5


def test_semantic_accuracy_requires_filters_and_time_range() -> None:
    case = BenchmarkCase(
        id="semantic-contract",
        question="查询近7天指定渠道交易金额",
        category="test",
        difficulty="medium",
        expected_status="success",
        gold_metric_ids=["transaction_amount"],
        gold_semantic_filters=[
            {"concept_id": "transaction_channel", "operator": "=", "value": "MOBILE"}
        ],
        gold_time_range={"kind": "relative_days", "days": 7},
        gold_tables=["dwd_card_transaction"],
        gold_sql="SELECT txn_amount_cny FROM dwd_card_transaction",
        expected_result_hash="a" * 64,
    )
    exact = EvaluationCaseResult(
        run_id="run",
        case_id=case.id,
        predicted_status="success",
        semantic_output={
            "metric_ids": ["transaction_amount"],
            "dimension_ids": [],
            "filters": case.gold_semantic_filters,
            "time_range": {
                "kind": "relative_days",
                "days": 7,
                "original_text": "近7天",
            },
        },
        success=True,
    )
    wrong_time = exact.model_copy(deep=True)
    wrong_time.semantic_output["time_range"]["days"] = 30  # type: ignore[index]
    assert (
        calculate_metrics("run", [case], [exact], strategy_variant="ontology_full")
        .semantic_query_accuracy
        == 1
    )
    detailed = calculate_metrics(
        "run", [case], [exact], strategy_variant="ontology_full"
    )
    assert detailed.semantic_parse_accuracy == 1
    assert detailed.metric_accuracy == 1
    assert detailed.dimension_accuracy == 1
    assert detailed.sql_asset_selection_accuracy is None
    assert (
        calculate_metrics("run", [case], [wrong_time], strategy_variant="ontology_full")
        .semantic_query_accuracy
        == 0
    )


class FixedStrategy:
    def execute(self, question: str) -> StrategyResult:
        return StrategyResult(
            question=question,
            query_mode="schema",
            strategy_variant="schema",
            status="success",
            generated_sql="SELECT transaction_id FROM dwd_card_transaction",
            selected_tables=["dwd_card_transaction"],
            selected_columns={"dwd_card_transaction": ["transaction_id"]},
            execution_result=ExecutionResult(
                columns=["transaction_id"],
                rows=[{"transaction_id": 1}],
                row_count=1,
            ),
        )


class RecordingRepository(MemoryEvaluationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.saved_case_order: list[str] = []

    def save_case(self, result: EvaluationCaseResult) -> None:
        super().save_case(result)
        self.saved_case_order.append(result.case_id)


def _service(
    repository: MemoryEvaluationRepository,
    ontology: OntologyService,
    *,
    settings: Settings | None = None,
    sql_asset_build_id: str | None = "sql-build",
) -> EvaluationService:
    return EvaluationService(
        repository,
        StrategyRouter({"schema": FixedStrategy()}),
        EvaluationPolicyInspector(ontology.bundle),
        settings or Settings(llm_mode="mock"),
        database_snapshot_hash="d" * 64,
        physical_rag_build_id="rag-build",
        ontology_version_id="ontology-version",
        bundle_hash="b" * 64,
        sql_asset_build_id=sql_asset_build_id,
    )


def test_evaluation_run_and_each_case_are_persisted_immediately(
    ontology: OntologyService,
) -> None:
    repository = RecordingRepository()
    service = _service(repository, ontology)
    run = service.create_run(
        EvaluationRunRequest(
            query_mode="schema",
            strategy_variant="schema",
            run_kind="smoke",
            max_cases=3,
        )
    )
    assert repository.get_run(run.run_id).status == "PENDING"  # type: ignore[union-attr]
    completed = service.execute_run(run.run_id, "data/benchmark/text2sql_v1.json")
    assert completed.status == "COMPLETED"
    assert len(repository.saved_case_order) == 3
    assert len(repository.list_cases(run.run_id)) == 3


def test_compare_rejects_critical_mismatch_even_for_legacy_override(
    ontology: OntologyService,
) -> None:
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    base: dict[str, Any] = {
        "query_mode": "schema",
        "strategy_variant": "schema",
        "model_provider": "mock",
        "model_name": "same-model",
        "git_commit_sha": "a" * 40,
        "database_snapshot_hash": "d" * 64,
        "benchmark_hash": "e" * 64,
        "benchmark_version": "v1",
        "status": "COMPLETED",
    }
    smoke = EvaluationRun(run_kind="smoke", **base)
    live = EvaluationRun(run_kind="live", **base)
    repository.save_run(smoke)
    repository.save_run(live)
    with pytest.raises(DataAssetAgentsError, match="run_kind"):
        service.compare(
            [smoke.run_id, live.run_id],
            "data/benchmark/text2sql_v1.json",
        )
    with pytest.raises(DataAssetAgentsError, match="run_kind"):
        service.compare(
            [smoke.run_id, live.run_id],
            "data/benchmark/text2sql_v1.json",
            allow_mismatch=True,
        )


@pytest.mark.parametrize("invalid_sha", ["unknown", "", "abc", "a" * 39, "g" * 40])
def test_compare_rejects_missing_or_invalid_git_sha_provenance(
    ontology: OntologyService,
    invalid_sha: str,
) -> None:
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    run = EvaluationRun(
        run_kind="live",
        query_mode="schema",
        strategy_variant="schema",
        model_provider="provider",
        model_name="model",
        git_commit_sha=invalid_sha,
        database_snapshot_hash="d" * 64,
        benchmark_hash="e" * 64,
        benchmark_version="v1",
        status="COMPLETED",
    )
    repository.save_run(run)

    with pytest.raises(
        DataAssetAgentsError,
        match="Fairness mismatch: valid git_commit_sha provenance is missing",
    ):
        service.compare([run.run_id], "data/benchmark/text2sql_v1.json")


def test_four_runs_with_same_valid_git_sha_can_be_compared(
    ontology: OntologyService,
) -> None:
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    run_ids: list[str] = []
    for index in range(4):
        run = EvaluationRun(
            run_id=f"valid-sha-run-{index}",
            run_kind="live",
            query_mode="schema",
            strategy_variant="schema",
            model_provider="provider",
            model_name="model",
            git_commit_sha="a" * 40,
            database_snapshot_hash="d" * 64,
            benchmark_hash="e" * 64,
            benchmark_version="v1",
            max_cases=1,
            status="COMPLETED",
        )
        repository.save_run(run)
        repository.save_case(
            EvaluationCaseResult(
                run_id=run.run_id,
                case_id="basic-01",
                predicted_status="success",
                success=True,
            )
        )
        run_ids.append(run.run_id)

    comparison = service.compare(run_ids, "data/benchmark/text2sql_v1.json")

    assert comparison.warnings == []
    assert len(comparison.runs) == 4


def test_two_factor_ontology_sources_require_distinct_version_bundle_and_build(
    ontology: OntologyService,
) -> None:
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    factors = (
        (
            "T-C",
            "ontology_no_sql_asset",
            "REVIEWED_O_C",
            "version-o-c",
            "a" * 64,
            None,
            "construction-o-c",
        ),
        (
            "T-D",
            "ontology_full",
            "REVIEWED_O_C",
            "version-o-c",
            "a" * 64,
            "build-o-c",
            "construction-o-c",
        ),
        (
            "T-E",
            "ontology_no_sql_asset",
            "REVIEWED_O_D",
            "version-o-d",
            "b" * 64,
            None,
            "construction-o-d",
        ),
        (
            "T-F",
            "ontology_full",
            "REVIEWED_O_D",
            "version-o-d",
            "b" * 64,
            "build-o-d",
            "construction-o-d",
        ),
        (
            "T-G",
            "ontology_no_sql_asset",
            "GOLD",
            "version-gold",
            "c" * 64,
            None,
            None,
        ),
        (
            "T-H",
            "ontology_full",
            "GOLD",
            "version-gold",
            "c" * 64,
            "build-gold",
            None,
        ),
    )
    run_ids: list[str] = []
    for group, variant, source, version, bundle, build, construction in factors:
        run = EvaluationRun(
            run_id=f"two-factor-{group.lower()}",
            run_kind="live",
            query_mode="ontology",
            strategy_variant=variant,
            sql_asset_enabled=variant == "ontology_full",
            model_provider="provider",
            model_name="model",
            git_commit_sha="a" * 40,
            database_snapshot_hash="d" * 64,
            benchmark_hash="e" * 64,
            benchmark_version="v1",
            ontology_version_id=version,
            bundle_hash=bundle,
            sql_asset_build_id=build,
            experiment_group=group,
            ontology_source=source,
            construction_run_id=construction,
            max_cases=1,
            status="COMPLETED",
        )
        repository.save_run(run)
        repository.save_case(
            EvaluationCaseResult(
                run_id=run.run_id,
                case_id="basic-01",
                predicted_status="success",
                success=True,
            )
        )
        run_ids.append(run.run_id)

    comparison = service.compare(run_ids, "data/benchmark/text2sql_v1.json")
    assert len(comparison.runs) == 6

    for run_id in ("two-factor-t-e", "two-factor-t-f"):
        shared = repository.get_run(run_id)
        assert shared is not None
        repository.save_run(shared.model_copy(update={"bundle_hash": "a" * 64}))
    with pytest.raises(DataAssetAgentsError, match="share a bundle"):
        service.compare(run_ids, "data/benchmark/text2sql_v1.json")


def test_live_run_requires_valid_git_sha(
    ontology: OntologyService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "unknown")
    service = _service(
        MemoryEvaluationRepository(),
        ontology,
        settings=Settings(llm_mode="live"),
    )

    with pytest.raises(
        DataAssetAgentsError,
        match="Live evaluation requires a valid git_commit_sha provenance",
    ):
        service.create_run(
            EvaluationRunRequest(
                query_mode="schema",
                strategy_variant="schema",
                run_kind="live",
            )
        )


def test_live_ontology_ablation_does_not_require_sql_asset_build(
    ontology: OntologyService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    service = _service(
        MemoryEvaluationRepository(),
        ontology,
        settings=Settings(llm_mode="live"),
        sql_asset_build_id=None,
    )

    run = service.create_run(
        EvaluationRunRequest(
            query_mode="ontology",
            strategy_variant="ontology_no_sql_asset",
            run_kind="live",
        )
    )

    assert run.sql_asset_build_id is None


def test_live_ontology_full_requires_ready_sql_asset_build(
    ontology: OntologyService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    service = _service(
        MemoryEvaluationRepository(),
        ontology,
        settings=Settings(llm_mode="live"),
        sql_asset_build_id=None,
    )

    with pytest.raises(DataAssetAgentsError, match="READY SQLAssetBuild"):
        service.create_run(
            EvaluationRunRequest(
                query_mode="ontology",
                strategy_variant="ontology_full",
                sql_asset_enabled=True,
                run_kind="live",
            )
        )


@pytest.mark.parametrize(
    ("field", "different"),
    [
        ("model_provider", "other-provider"),
        ("model_name", "other-model"),
        ("temperature", 0.2),
        ("max_output_tokens", 4096),
        ("timeout_seconds", 60),
        ("git_commit_sha", "b" * 40),
        ("benchmark_hash", "f" * 64),
        ("database_snapshot_hash", "a" * 64),
        ("prompt_version", "v2"),
        ("strategy_version", "v2"),
        ("random_seed", 7),
        ("concurrency", 2),
        ("max_cases", 5),
    ],
)
def test_compare_rejects_every_shared_reproducibility_mismatch(
    ontology: OntologyService,
    field: str,
    different: object,
) -> None:
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    base = EvaluationRun(
        run_kind="live",
        query_mode="schema",
        strategy_variant="schema",
        model_provider="provider",
        model_name="model",
        git_commit_sha="a" * 40,
        database_snapshot_hash="d" * 64,
        benchmark_hash="e" * 64,
        benchmark_version="v1",
        status="COMPLETED",
    )
    changed = base.model_copy(update={field: different, "run_id": "changed-run"})
    repository.save_run(base)
    repository.save_run(changed)
    with pytest.raises(DataAssetAgentsError, match=field):
        service.compare([base.run_id, changed.run_id], "data/benchmark/text2sql_v1.json")


def test_ontology_runs_record_exact_bundle_provenance(ontology: OntologyService) -> None:
    service = _service(MemoryEvaluationRepository(), ontology)
    without_assets = service.create_run(
        EvaluationRunRequest(
            query_mode="ontology",
            strategy_variant="ontology_no_sql_asset",
            run_kind="smoke",
        )
    )
    full = service.create_run(
        EvaluationRunRequest(
            query_mode="ontology",
            strategy_variant="ontology_full",
            sql_asset_enabled=True,
            run_kind="smoke",
        )
    )
    assert without_assets.bundle_hash == "b" * 64
    assert full.bundle_hash == "b" * 64
    assert without_assets.sql_asset_build_id is None
    assert full.sql_asset_build_id == "sql-build"


def test_compare_rejects_completed_run_with_incomplete_case_coverage(
    ontology: OntologyService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    repository = MemoryEvaluationRepository()
    service = _service(repository, ontology)
    run = service.create_run(
        EvaluationRunRequest(
            query_mode="schema",
            strategy_variant="schema",
            run_kind="smoke",
            max_cases=2,
        )
    )
    repository.save_run(run.model_copy(update={"status": "COMPLETED"}))
    with pytest.raises(DataAssetAgentsError, match="case coverage"):
        service.compare([run.run_id], "data/benchmark/text2sql_v1.json")


def _formal_group_response() -> dict[str, Any]:
    return {
        "run": {
            "run_id": "formal-t-a",
            "status": "COMPLETED",
            "experiment_group": "T-A",
            "strategy_variant": "schema",
            "sql_asset_enabled": False,
            "ontology_source": "NONE",
            "run_kind": "live",
            "concurrency": 1,
            "max_cases": None,
        },
        "metrics": {"case_count": 80},
    }


def test_formal_runner_resumes_only_complete_eighty_case_group() -> None:
    cases = [{"case_id": f"case-{index}"} for index in range(80)]
    row = _validate_group_row(
        GROUPS[0],
        _formal_group_response(),
        cases,
        concurrency=1,
    )
    assert row["run"]["run_id"] == "formal-t-a"
    assert len(row["cases"]) == 80

    with pytest.raises(RuntimeError, match="79 cases instead of 80"):
        _validate_group_row(
            GROUPS[0],
            _formal_group_response(),
            cases[:-1],
            concurrency=1,
        )


def test_formal_runner_progress_is_atomic_and_schema_checked(tmp_path: Path) -> None:
    progress = tmp_path / "progress.json"
    payload = {
        "progress_schema_version": "1.0",
        "status": "IN_PROGRESS",
        "completed_groups": {"T-A": "formal-t-a"},
        "failed_attempts": [],
    }
    _atomic_write_json(progress, payload)
    assert _load_progress(progress) == payload
    assert not (tmp_path / "progress.json.tmp").exists()

    _atomic_write_json(progress, {"progress_schema_version": "unsupported"})
    with pytest.raises(RuntimeError, match="Unsupported resume manifest"):
        _load_progress(progress)


def test_formal_runner_reports_paired_case_outcomes_and_metric_delta() -> None:
    groups = {
        "T-C": {
            "metrics": {"result_hash_accuracy": 0.5},
            "cases": [
                {
                    "case_id": "case-a",
                    "question": "A",
                    "success": False,
                    "failure_category": "SEMANTIC_PARSE_ERROR",
                },
                {
                    "case_id": "case-b",
                    "question": "B",
                    "success": True,
                    "failure_category": None,
                },
            ],
        },
        "T-G": {
            "metrics": {"result_hash_accuracy": 0.75},
            "cases": [
                {
                    "case_id": "case-a",
                    "question": "A",
                    "success": True,
                    "failure_category": None,
                },
                {
                    "case_id": "case-b",
                    "question": "B",
                    "success": False,
                    "failure_category": "RESULT_MISMATCH",
                },
            ],
        },
    }

    comparison = _case_outcome_comparison(groups, "T-C", "T-G")

    assert _result_delta(groups, "T-C", "T-G") == -0.25
    assert comparison["left_failed_right_succeeded_count"] == 1
    assert comparison["left_failed_right_succeeded"][0]["case_id"] == "case-a"
    assert comparison["left_succeeded_right_failed_count"] == 1
    assert comparison["left_succeeded_right_failed"][0]["case_id"] == "case-b"
