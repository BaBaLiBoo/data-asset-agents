from datetime import date
from decimal import Decimal
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
) -> EvaluationService:
    return EvaluationService(
        repository,
        StrategyRouter({"schema": FixedStrategy()}),
        EvaluationPolicyInspector(ontology.bundle),
        Settings(llm_mode="mock"),
        database_snapshot_hash="d" * 64,
        physical_rag_build_id="rag-build",
        ontology_version_id="ontology-version",
        bundle_hash="b" * 64,
        sql_asset_build_id="sql-build",
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
        "git_commit_sha": "abc",
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


@pytest.mark.parametrize(
    ("field", "different"),
    [
        ("model_provider", "other-provider"),
        ("model_name", "other-model"),
        ("temperature", 0.2),
        ("max_output_tokens", 4096),
        ("timeout_seconds", 60),
        ("git_commit_sha", "def"),
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
        git_commit_sha="abc",
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
) -> None:
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
