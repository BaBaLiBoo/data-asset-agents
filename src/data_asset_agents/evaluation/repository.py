from __future__ import annotations

from typing import Protocol

from sqlalchemy import Engine, text

from data_asset_agents.evaluation.models import EvaluationCaseResult, EvaluationRun


class EvaluationRepository(Protocol):
    def save_run(self, run: EvaluationRun) -> None: ...
    def get_run(self, run_id: str) -> EvaluationRun | None: ...
    def list_runs(self, limit: int = 100) -> list[EvaluationRun]: ...
    def save_case(self, result: EvaluationCaseResult) -> None: ...
    def list_cases(self, run_id: str) -> list[EvaluationCaseResult]: ...


class MemoryEvaluationRepository:
    def __init__(self) -> None:
        self.runs: dict[str, EvaluationRun] = {}
        self.cases: dict[tuple[str, str], EvaluationCaseResult] = {}

    def save_run(self, run: EvaluationRun) -> None:
        self.runs[run.run_id] = run.model_copy(deep=True)

    def get_run(self, run_id: str) -> EvaluationRun | None:
        run = self.runs.get(run_id)
        return run.model_copy(deep=True) if run else None

    def list_runs(self, limit: int = 100) -> list[EvaluationRun]:
        return sorted(
            (run.model_copy(deep=True) for run in self.runs.values()),
            key=lambda run: run.started_at or run.completed_at or run.run_id,
            reverse=True,
        )[:limit]

    def save_case(self, result: EvaluationCaseResult) -> None:
        self.cases[(result.run_id, result.case_id)] = result.model_copy(deep=True)

    def list_cases(self, run_id: str) -> list[EvaluationCaseResult]:
        return sorted(
            (
                result.model_copy(deep=True)
                for (stored_run_id, _), result in self.cases.items()
                if stored_run_id == run_id
            ),
            key=lambda result: result.created_at,
        )


class PostgresEvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save_run(self, run: EvaluationRun) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO evaluation_run
                        (run_id, run_kind, query_mode, strategy_variant, status,
                         benchmark_version, database_snapshot_hash, model_name, payload,
                         started_at, completed_at)
                    VALUES
                        (:run_id, :run_kind, :query_mode, :strategy_variant, :status,
                         :benchmark_version, :database_snapshot_hash, :model_name,
                         CAST(:payload AS JSONB), :started_at, :completed_at)
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        payload = EXCLUDED.payload,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at
                    """
                ),
                {
                    "run_id": run.run_id,
                    "run_kind": run.run_kind,
                    "query_mode": run.query_mode,
                    "strategy_variant": run.strategy_variant,
                    "status": run.status,
                    "benchmark_version": run.benchmark_version,
                    "database_snapshot_hash": run.database_snapshot_hash,
                    "model_name": run.model_name,
                    "payload": run.model_dump_json(),
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                },
            )

    def get_run(self, run_id: str) -> EvaluationRun | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                text("SELECT payload FROM evaluation_run WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one_or_none()
        return EvaluationRun.model_validate(payload) if payload is not None else None

    def list_runs(self, limit: int = 100) -> list[EvaluationRun]:
        with self.engine.connect() as connection:
            payloads = connection.execute(
                text(
                    "SELECT payload FROM evaluation_run "
                    "ORDER BY COALESCE(started_at, completed_at) DESC NULLS LAST, run_id "
                    "LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
            return [EvaluationRun.model_validate(payload) for payload in payloads]

    def save_case(self, result: EvaluationCaseResult) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO evaluation_case_result
                        (run_id, case_id, status, predicted_status, success,
                         failure_category, payload, created_at)
                    VALUES
                        (:run_id, :case_id, :status, :predicted_status, :success,
                         :failure_category, CAST(:payload AS JSONB), :created_at)
                    ON CONFLICT (run_id, case_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        predicted_status = EXCLUDED.predicted_status,
                        success = EXCLUDED.success,
                        failure_category = EXCLUDED.failure_category,
                        payload = EXCLUDED.payload,
                        created_at = EXCLUDED.created_at
                    """
                ),
                {
                    "run_id": result.run_id,
                    "case_id": result.case_id,
                    "status": result.status,
                    "predicted_status": result.predicted_status,
                    "success": result.success,
                    "failure_category": result.failure_category,
                    "payload": result.model_dump_json(),
                    "created_at": result.created_at,
                },
            )

    def list_cases(self, run_id: str) -> list[EvaluationCaseResult]:
        with self.engine.connect() as connection:
            payloads = connection.execute(
                text(
                    "SELECT payload FROM evaluation_case_result "
                    "WHERE run_id = :run_id ORDER BY created_at, case_id"
                ),
                {"run_id": run_id},
            ).scalars()
            return [EvaluationCaseResult.model_validate(payload) for payload in payloads]
