from __future__ import annotations

import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.evaluation.benchmark import benchmark_source_hash, load_benchmark
from data_asset_agents.evaluation.metrics import calculate_metrics, normalize_join
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
        bundle_hash: str | None,
        sql_asset_build_id: str | None,
    ) -> None:
        self.repository = repository
        self.strategies = strategies
        self.inspector = inspector
        self.settings = settings
        self.database_snapshot_hash = database_snapshot_hash
        self.physical_rag_build_id = physical_rag_build_id
        self.ontology_version_id = ontology_version_id
        self.bundle_hash = bundle_hash
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
        source_hash = benchmark_source_hash(benchmark_path)
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
            timeout_seconds=self.settings.llm_timeout_seconds,
            git_commit_sha=_git_commit_sha(),
            database_snapshot_hash=self.database_snapshot_hash,
            benchmark_hash=source_hash,
            physical_rag_build_id=(
                self.physical_rag_build_id if request.strategy_variant == "rag" else None
            ),
            ontology_version_id=(
                self.ontology_version_id
                if request.strategy_variant.startswith("ontology")
                else None
            ),
            bundle_hash=(
                self.bundle_hash if request.strategy_variant.startswith("ontology") else None
            ),
            sql_asset_build_id=(
                self.sql_asset_build_id if request.strategy_variant == "ontology_full" else None
            ),
            benchmark_version=suite.version + ":" + source_hash[:12],
            benchmark_path=str(benchmark_path.relative_to(Path.cwd())),
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
            failure_category, failure_reason = self._failure(
                case, output, result_hash, policy.violations if policy else []
            )
            success = status_matches and result_matches and failure_category is None
            selected_asset_id = (
                str(output.selected_sql_asset.get("id")) if output.selected_sql_asset else None
            )
            retrieved_tables, retrieved_columns = self._retrieved_physical_assets(
                output.retrieved_context
            )
            return EvaluationCaseResult(
                run_id=run.run_id,
                case_id=case.id,
                question=case.question,
                category=case.category,
                difficulty=case.difficulty,
                gold={
                    "expected_status": case.expected_status,
                    "metric_ids": case.gold_metric_ids,
                    "dimension_ids": case.gold_dimension_ids,
                    "semantic_filters": case.gold_semantic_filters,
                    "time_range": case.gold_time_range,
                    "tables": case.gold_tables,
                    "columns": case.gold_columns,
                    "joins": case.gold_joins,
                    "sql": case.gold_sql,
                    "result_hash": case.expected_result_hash,
                },
                predicted_status=output.status,
                semantic_output=(
                    output.semantic_query.model_dump(mode="json") if output.semantic_query else None
                ),
                retrieved_context=output.retrieved_context,
                retrieved_tables=retrieved_tables,
                retrieved_columns=retrieved_columns,
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
                template_compatible=(
                    output.selected_sql_asset is not None
                    if run.strategy_variant == "ontology_full"
                    else None
                ),
                selected_sql_asset_id=selected_asset_id,
                success=success,
                failure_category=failure_category,
                failure_reason=failure_reason,
            )
        except Exception as exc:
            failure_category = "TIMEOUT" if isinstance(exc, TimeoutError) else "PROVIDER_ERROR"
            return EvaluationCaseResult(
                run_id=run.run_id,
                case_id=case.id,
                question=case.question,
                category=case.category,
                difficulty=case.difficulty,
                gold={
                    "expected_status": case.expected_status,
                    "sql": case.gold_sql,
                    "result_hash": case.expected_result_hash,
                },
                status="FAILED",
                predicted_status="failed",
                success=False,
                failure_category=failure_category,
                failure_reason=str(exc),
            )

    @staticmethod
    def _retrieved_physical_assets(
        context: list[dict[str, Any]] | None,
    ) -> tuple[list[str], list[str]]:
        """Extract ranked physical evidence without interpreting semantic concepts."""

        tables: list[str] = []
        columns: list[str] = []
        for item in context or []:
            document = item.get("document", item)
            if not isinstance(document, dict):
                continue
            table = document.get("table")
            column = document.get("column")
            if isinstance(table, str) and table not in tables:
                tables.append(table)
            qualified = (
                f"{table}.{column}"
                if isinstance(table, str) and isinstance(column, str)
                else None
            )
            if qualified and qualified not in columns:
                columns.append(qualified)
        return tables, columns

    @staticmethod
    def _failure(
        case: BenchmarkCase,
        output: Any,
        result_hash: str | None,
        policy_violations: list[str],
    ) -> tuple[str | None, str | None]:
        if output.status != case.expected_status:
            if output.error_code == "TEMPLATE_INCOMPATIBLE":
                return "TEMPLATE_INCOMPATIBLE", output.unsupported_reason
            if case.expected_status == "success" and not output.selected_tables:
                return "TABLE_SELECTION_ERROR", "strategy produced no usable physical assets"
            category = (
                "CLARIFICATION_ERROR"
                if case.expected_status == "clarification_required"
                else (
                    "UNSUPPORTED_CLASSIFICATION_ERROR"
                    if case.expected_status == "unsupported"
                    else "SEMANTIC_PARSE_ERROR"
                )
            )
            return category, f"expected {case.expected_status}, got {output.status}"
        if output.common_validation_report and not output.common_validation_report.valid:
            reason = "; ".join(output.common_validation_report.errors)
            lowered = reason.lower()
            if "parse" in lowered:
                category = "SQL_PARSE_ERROR"
            elif "explain" in lowered:
                category = "EXPLAIN_ERROR"
            elif "does not exist" in lowered or "execution" in lowered:
                category = "EXECUTION_ERROR"
            elif "column" in lowered:
                category = "COLUMN_SELECTION_ERROR"
            elif "table" in lowered:
                category = "TABLE_SELECTION_ERROR"
            else:
                category = "SQL_PARSE_ERROR"
            return category, reason
        if policy_violations:
            reason = "; ".join(policy_violations)
            lowered = reason.lower()
            if any(item in lowered for item in ("deprecated", "temporary", "test")):
                category = "TABLE_SELECTION_ERROR"
            elif "join" in lowered:
                category = "JOIN_ERROR"
            elif any(item in lowered for item in ("amount field", "time field", "column")):
                category = "COLUMN_SELECTION_ERROR"
            else:
                category = "BUSINESS_POLICY_ERROR"
            return category, reason
        if set(output.selected_tables) != set(case.gold_tables):
            return "TABLE_SELECTION_ERROR", "generated SQL table set differs from Gold"
        referenced_columns = {
            f"{table}.{column}"
            for table, columns in output.selected_columns.items()
            for column in columns
        }
        if referenced_columns != set(case.gold_columns):
            return "COLUMN_SELECTION_ERROR", "generated SQL column set differs from Gold"
        if case.gold_joins and {
            normalize_join(item) for item in output.discovered_joins
        } != {normalize_join(item) for item in case.gold_joins}:
            return "JOIN_ERROR", "generated SQL Join set differs from Gold"
        if case.expected_result_hash and result_hash != case.expected_result_hash:
            return "RESULT_MISMATCH", "execution result hash differs from Gold"
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

    def metrics_for_run(self, run_id: str) -> EvaluationMetrics:
        """Calculate metrics using the exact allow-listed benchmark stored by the run."""

        run = self.repository.get_run(run_id)
        if run is None:
            raise DataAssetAgentsError(f"Evaluation run not found: {run_id}")
        return self.metrics(run_id, run.benchmark_path)

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
        if any(run.status != "COMPLETED" for run in present):
            raise DataAssetAgentsError("Only COMPLETED evaluation runs can be compared")
        if any(run.benchmark_hash == "0" * 64 for run in present):
            raise DataAssetAgentsError("Fairness mismatch: benchmark_hash provenance is missing")
        warnings: list[str] = []
        fairness = {
            "run_kind": {run.run_kind for run in present},
            "model_provider": {run.model_provider for run in present},
            "benchmark_version": {run.benchmark_version for run in present},
            "benchmark_hash": {run.benchmark_hash for run in present},
            "database_snapshot_hash": {run.database_snapshot_hash for run in present},
            "model_name": {run.model_name for run in present},
            "temperature": {run.temperature for run in present},
            "max_output_tokens": {run.max_output_tokens for run in present},
            "timeout_seconds": {run.timeout_seconds for run in present},
            "git_commit_sha": {run.git_commit_sha for run in present},
            "prompt_version": {run.prompt_version for run in present},
            "strategy_version": {run.strategy_version for run in present},
            "random_seed": {run.random_seed for run in present},
            "concurrency": {run.concurrency for run in present},
            "max_cases": {run.max_cases for run in present},
        }
        ontology_runs = [run for run in present if run.strategy_variant.startswith("ontology")]
        if ontology_runs:
            if any(not run.bundle_hash for run in ontology_runs):
                raise DataAssetAgentsError("Fairness mismatch: bundle_hash provenance is missing")
            fairness["ontology_version_id"] = {
                run.ontology_version_id for run in ontology_runs
            }
            fairness["bundle_hash"] = {run.bundle_hash for run in ontology_runs}
        for field, values in fairness.items():
            if len(values) > 1:
                warnings.append(f"Fairness mismatch: {field}")
        # Kept in the call signature for API compatibility. Critical provenance
        # mismatches are never downgraded to warnings, even when an older caller
        # still sends allow_mismatch=true.
        _ = allow_mismatch
        if warnings:
            raise DataAssetAgentsError("; ".join(warnings))
        case_id_sets: list[set[str]] = []
        for run in present:
            suite = load_benchmark(self._allowlisted_benchmark(run.benchmark_path))
            expected_cases = suite.cases[: run.max_cases] if run.max_cases else suite.cases
            expected_ids = {case.id for case in expected_cases}
            actual_ids = {case.case_id for case in self.repository.list_cases(run.run_id)}
            if actual_ids != expected_ids:
                raise DataAssetAgentsError(
                    f"Evaluation run {run.run_id} has incomplete or unexpected case coverage"
                )
            case_id_sets.append(actual_ids)
        if len({frozenset(case_ids) for case_ids in case_id_sets}) > 1:
            raise DataAssetAgentsError("Fairness mismatch: evaluated case IDs")
        return EvaluationComparison(
            runs=[self.metrics_for_run(run.run_id) for run in present],
            warnings=warnings,
        )


def database_snapshot_hash(catalog_json: str, seed_path: Path | str) -> str:
    seed = Path(seed_path).read_bytes()
    return hashlib.sha256(catalog_json.encode("utf-8") + seed).hexdigest()


def _git_commit_sha() -> str:
    configured = os.getenv("GITHUB_SHA")
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
