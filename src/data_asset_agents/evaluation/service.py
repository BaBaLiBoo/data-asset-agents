from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.evaluation.benchmark import benchmark_source_hash, load_benchmark
from data_asset_agents.evaluation.metrics import calculate_metrics
from data_asset_agents.evaluation.models import (
    BenchmarkCase,
    EvaluationCaseResult,
    EvaluationComparison,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationRunRequest,
)
from data_asset_agents.evaluation.normalizer import ResultNormalizer
from data_asset_agents.evaluation.repository import EvaluationRepository
from data_asset_agents.evaluation.strategies import StrategyRouter
from data_asset_agents.text2sql.models import QueryFilter
from data_asset_agents.validation import EvaluationPolicyInspector


class EvaluationService:
    """Run reproducible evaluations and persist every completed case immediately."""

    def __init__(
        self,
        repository: EvaluationRepository,
        strategies: StrategyRouter,
        inspector: EvaluationPolicyInspector,
        settings: Settings,
        *,
        database_snapshot_hash: str,
        physical_rag_build_id: str | None,
        ontology_version_id: str | None,
        sql_asset_build_id: str | None,
    ) -> None:
        self.repository = repository
        self.strategies = strategies
        self.inspector = inspector
        self.settings = settings
        self.database_snapshot_hash = database_snapshot_hash
        self.physical_rag_build_id = physical_rag_build_id
        self.ontology_version_id = ontology_version_id
        self.sql_asset_build_id = sql_asset_build_id
        self.normalizer = ResultNormalizer()

    def create_run(self, request: EvaluationRunRequest) -> EvaluationRun:
        benchmark_path = self._allowlisted_benchmark(request.benchmark_path)
        suite = load_benchmark(benchmark_path)
        if request.run_kind == "live" and self.settings.llm_mode != "live":
            raise DataAssetAgentsError("live evaluation requires LLM_MODE=live")
        if request.run_kind == "smoke" and self.settings.llm_mode != "mock":
            raise DataAssetAgentsError("smoke evaluation requires LLM_MODE=mock")
        expected_mode = {
            "schema": "schema",
            "rag": "rag",
            "ontology_no_sql_asset": "ontology",
            "ontology_full": "ontology",
        }[request.strategy_variant]
        if request.query_mode != expected_mode:
            raise DataAssetAgentsError("query_mode does not match strategy_variant")
        provider = request.model_provider or self.settings.llm_provider
        model_name = request.model_name or self.settings.llm_model
        run = EvaluationRun(
            run_kind=request.run_kind,
            query_mode=request.query_mode,
            strategy_variant=request.strategy_variant,
            sql_asset_enabled=request.sql_asset_enabled,
            model_provider=provider,
            model_name=model_name,
            embedding_model=(
                self.settings.embedding_model if request.strategy_variant != "schema" else None
            ),
            temperature=0,
            max_output_tokens=self.settings.llm_max_output_tokens,
            git_commit_sha=os.getenv("GITHUB_SHA", "workspace-uncommitted"),
            database_snapshot_hash=self.database_snapshot_hash,
            physical_rag_build_id=(
                self.physical_rag_build_id if request.strategy_variant == "rag" else None
            ),
            ontology_version_id=(
                self.ontology_version_id
                if request.strategy_variant.startswith("ontology")
                else None
            ),
            sql_asset_build_id=(
                self.sql_asset_build_id if request.strategy_variant == "ontology_full" else None
            ),
            benchmark_version=suite.version + ":" + benchmark_source_hash(benchmark_path)[:12],
            max_cases=request.max_cases,
            concurrency=request.concurrency,
        )
        self.repository.save_run(run)
        return run

    @staticmethod
    def _allowlisted_benchmark(path: str) -> Path:
        configured_root = Path("data/benchmark").resolve()
        resolved = Path(path).resolve()
        if configured_root not in resolved.parents:
            raise DataAssetAgentsError("benchmark_path must be inside data/benchmark")
        return resolved

    def execute_run(self, run_id: str, benchmark_path: str) -> EvaluationRun:
        run = self.repository.get_run(run_id)
        if run is None:
            raise DataAssetAgentsError(f"Evaluation run not found: {run_id}")
        suite = load_benchmark(self._allowlisted_benchmark(benchmark_path))
        cases = suite.cases[: run.max_cases] if run.max_cases else suite.cases
        running = run.mark_running()
        self.repository.save_run(running)
        try:
            if running.concurrency == 1:
                for case in cases:
                    self.repository.save_case(self._evaluate_case(running, case))
            else:
                with ThreadPoolExecutor(max_workers=running.concurrency) as pool:
                    futures = {
                        pool.submit(self._evaluate_case, running, case): case for case in cases
                    }
                    for future in as_completed(futures):
                        self.repository.save_case(future.result())
            completed = running.model_copy(
                update={"status": "COMPLETED", "completed_at": datetime.now(UTC)}
            )
            self.repository.save_run(completed)
            return completed
        except Exception as exc:
            failed = running.model_copy(
                update={
                    "status": "FAILED",
                    "completed_at": datetime.now(UTC),
                    "error_message": str(exc),
                }
            )
            self.repository.save_run(failed)
            raise

    def _evaluate_case(self, run: EvaluationRun, case: BenchmarkCase) -> EvaluationCaseResult:
        try:
            strategy = self.strategies.strategies[run.strategy_variant]
            output = strategy.execute(case.question)
            policy = None
            if output.generated_sql:
                policy = self.inspector.inspect(
                    output.generated_sql,
                    semantic_query=output.semantic_query,
                    required_filters=[
                        QueryFilter.model_validate(item) for item in case.gold_filters
                    ],
                )
            result_hash = (
                self.normalizer.hash(
                    output.execution_result,
                    order_sensitive=case.result_order_sensitive,
                )
                if output.execution_result
                else None
            )
            status_matches = output.status == case.expected_status
            result_matches = (
                result_hash == case.expected_result_hash
                if case.expected_result_hash is not None
                else True
            )
            success = status_matches and result_matches
            failure_category, failure_reason = self._failure(
                case, output, result_hash, policy.violations if policy else []
            )
            selected_asset_id = (
                str(output.selected_sql_asset.get("id")) if output.selected_sql_asset else None
            )
            return EvaluationCaseResult(
                run_id=run.run_id,
                case_id=case.id,
                predicted_status=output.status,
                semantic_output=(
                    output.semantic_query.model_dump(mode="json") if output.semantic_query else None
                ),
                retrieved_context=output.retrieved_context,
                raw_model_output=output.raw_model_output,
                generated_sql=output.generated_sql,
                referenced_tables=output.selected_tables,
                referenced_columns=sorted(
                    f"{table}.{column}"
                    for table, columns in output.selected_columns.items()
                    for column in columns
                ),
                discovered_joins=output.discovered_joins,
                common_validation_errors=(
                    output.common_validation_report.errors
                    if output.common_validation_report
                    else []
                ),
                ontology_policy_errors=(
                    output.ontology_policy_report.errors if output.ontology_policy_report else None
                ),
                evaluation_policy_violations=policy.violations if policy else [],
                execution_result_hash=result_hash,
                latency_ms=output.latency_ms,
                token_usage=output.token_usage,
                template_adopted=(
                    bool(output.sql_rewrite and output.sql_rewrite.get("used_template"))
                    if run.strategy_variant == "ontology_full"
                    else None
                ),
                selected_sql_asset_id=selected_asset_id,
                success=success,
                failure_category=failure_category,
                failure_reason=failure_reason,
            )
        except Exception as exc:
            return EvaluationCaseResult(
                run_id=run.run_id,
                case_id=case.id,
                status="FAILED",
                predicted_status="failed",
                success=False,
                failure_category="model_error",
                failure_reason=str(exc),
            )

    @staticmethod
    def _failure(
        case: BenchmarkCase,
        output: Any,
        result_hash: str | None,
        policy_violations: list[str],
    ) -> tuple[str | None, str | None]:
        if output.status != case.expected_status:
            category = (
                "clarification_error"
                if case.expected_status == "clarification_required"
                else (
                    "unsupported_error"
                    if case.expected_status == "unsupported"
                    else "semantic_parse_error"
                )
            )
            return category, f"expected {case.expected_status}, got {output.status}"
        if output.common_validation_report and not output.common_validation_report.valid:
            return "validation_error", "; ".join(output.common_validation_report.errors)
        if policy_violations:
            return "business_policy_error", "; ".join(policy_violations)
        if case.expected_result_hash and result_hash != case.expected_result_hash:
            return "result_mismatch", "execution result hash differs from Gold"
        return None, None

    def metrics(self, run_id: str, benchmark_path: str) -> EvaluationMetrics:
        run = self.repository.get_run(run_id)
        if run is None:
            raise DataAssetAgentsError(f"Evaluation run not found: {run_id}")
        suite = load_benchmark(self._allowlisted_benchmark(benchmark_path))
        cases = suite.cases[: run.max_cases] if run.max_cases else suite.cases
        return calculate_metrics(
            run_id,
            cases,
            self.repository.list_cases(run_id),
            strategy_variant=run.strategy_variant,
        )

    def compare(
        self,
        run_ids: list[str],
        benchmark_path: str,
        *,
        allow_mismatch: bool = False,
    ) -> EvaluationComparison:
        runs = [self.repository.get_run(run_id) for run_id in run_ids]
        if any(run is None for run in runs):
            raise DataAssetAgentsError("One or more evaluation runs do not exist")
        present = [run for run in runs if run is not None]
        warnings: list[str] = []
        fairness = {
            "run_kind": {run.run_kind for run in present},
            "benchmark_version": {run.benchmark_version for run in present},
            "database_snapshot_hash": {run.database_snapshot_hash for run in present},
            "model_name": {run.model_name for run in present},
        }
        for field, values in fairness.items():
            if len(values) > 1:
                warnings.append(f"Fairness mismatch: {field}")
        if warnings and not allow_mismatch:
            raise DataAssetAgentsError("; ".join(warnings))
        return EvaluationComparison(
            runs=[self.metrics(run.run_id, benchmark_path) for run in present],
            warnings=warnings,
        )


def database_snapshot_hash(catalog_json: str, seed_path: Path | str) -> str:
    seed = Path(seed_path).read_bytes()
    return hashlib.sha256(catalog_json.encode("utf-8") + seed).hexdigest()
