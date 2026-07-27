"""Persistence for immutable construction runs and reviewable candidates."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from typing import Protocol

from sqlalchemy import Engine, text

from data_asset_agents.core.errors import OntologyConflictError

from .models import ConstructionCandidate, OntologyConstructionRun


class ConstructionRepository(Protocol):
    def save_run(self, run: OntologyConstructionRun) -> None: ...
    def list_runs(self) -> list[OntologyConstructionRun]: ...
    def get_run(self, run_id: str) -> OntologyConstructionRun | None: ...
    def save_candidates(self, candidates: list[ConstructionCandidate]) -> None: ...
    def list_candidates(self, run_id: str) -> list[ConstructionCandidate]: ...
    def get_candidate(
        self, run_id: str, candidate_id: str
    ) -> ConstructionCandidate | None: ...
    def save_review(
        self, candidate: ConstructionCandidate, *, expected_revision: int
    ) -> None: ...


class MemoryConstructionRepository:
    def __init__(self) -> None:
        self.runs: dict[str, OntologyConstructionRun] = {}
        self.candidates: dict[tuple[str, str], ConstructionCandidate] = {}
        self._lock = threading.RLock()

    def save_run(self, run: OntologyConstructionRun) -> None:
        with self._lock:
            self.runs[run.run_id] = deepcopy(run)

    def list_runs(self) -> list[OntologyConstructionRun]:
        return sorted(
            deepcopy(list(self.runs.values())),
            key=lambda item: item.started_at,
            reverse=True,
        )

    def get_run(self, run_id: str) -> OntologyConstructionRun | None:
        value = self.runs.get(run_id)
        return deepcopy(value) if value else None

    def save_candidates(self, candidates: list[ConstructionCandidate]) -> None:
        with self._lock:
            for candidate in candidates:
                self.candidates[(candidate.run_id, candidate.candidate_id)] = deepcopy(
                    candidate
                )

    def list_candidates(self, run_id: str) -> list[ConstructionCandidate]:
        return sorted(
            [
                deepcopy(candidate)
                for (candidate_run, _), candidate in self.candidates.items()
                if candidate_run == run_id
            ],
            key=lambda item: (item.resource_type, item.candidate_id),
        )

    def get_candidate(
        self, run_id: str, candidate_id: str
    ) -> ConstructionCandidate | None:
        value = self.candidates.get((run_id, candidate_id))
        return deepcopy(value) if value else None

    def save_review(
        self, candidate: ConstructionCandidate, *, expected_revision: int
    ) -> None:
        with self._lock:
            current = self.candidates.get((candidate.run_id, candidate.candidate_id))
            if current is None or current.revision != expected_revision:
                raise OntologyConflictError("Construction candidate revision conflict")
            self.candidates[(candidate.run_id, candidate.candidate_id)] = deepcopy(candidate)


class PostgresConstructionRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _payload(value: object) -> str:
        return json.dumps(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value,
            ensure_ascii=False,
        )

    def save_run(self, run: OntologyConstructionRun) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO ontology_construction_run(
                        run_id, source_snapshot_id, status, payload, updated_at
                    ) VALUES (
                        :run_id, :snapshot_id, :status, CAST(:payload AS jsonb), now()
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        status=EXCLUDED.status,
                        payload=EXCLUDED.payload,
                        updated_at=now()
                """),
                {
                    "run_id": run.run_id,
                    "snapshot_id": run.source_snapshot_id,
                    "status": run.status,
                    "payload": self._payload(run),
                },
            )

    def list_runs(self) -> list[OntologyConstructionRun]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM ontology_construction_run "
                    "ORDER BY updated_at DESC"
                )
            ).scalars()
            return [OntologyConstructionRun.model_validate(row) for row in rows]

    def get_run(self, run_id: str) -> OntologyConstructionRun | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT payload FROM ontology_construction_run WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            ).scalar_one_or_none()
            return OntologyConstructionRun.model_validate(row) if row else None

    def save_candidates(self, candidates: list[ConstructionCandidate]) -> None:
        with self.engine.begin() as connection:
            for candidate in candidates:
                connection.execute(
                    text("""
                        INSERT INTO ontology_construction_candidate(
                            run_id,candidate_id,resource_type,status,revision,payload,updated_at
                        ) VALUES (
                            :run_id,:candidate_id,:resource_type,:status,:revision,
                            CAST(:payload AS jsonb),now()
                        )
                        ON CONFLICT (run_id,candidate_id) DO UPDATE SET
                            resource_type=EXCLUDED.resource_type,
                            status=EXCLUDED.status,
                            revision=EXCLUDED.revision,
                            payload=EXCLUDED.payload,
                            updated_at=now()
                    """),
                    {
                        **candidate.model_dump(mode="python"),
                        "payload": self._payload(candidate),
                    },
                )

    def list_candidates(self, run_id: str) -> list[ConstructionCandidate]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM ontology_construction_candidate "
                    "WHERE run_id=:run_id ORDER BY resource_type,candidate_id"
                ),
                {"run_id": run_id},
            ).scalars()
            return [ConstructionCandidate.model_validate(row) for row in rows]

    def get_candidate(
        self, run_id: str, candidate_id: str
    ) -> ConstructionCandidate | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT payload FROM ontology_construction_candidate "
                    "WHERE run_id=:run_id AND candidate_id=:candidate_id"
                ),
                {"run_id": run_id, "candidate_id": candidate_id},
            ).scalar_one_or_none()
            return ConstructionCandidate.model_validate(row) if row else None

    def save_review(
        self, candidate: ConstructionCandidate, *, expected_revision: int
    ) -> None:
        with self.engine.begin() as connection:
            updated = connection.execute(
                text("""
                    UPDATE ontology_construction_candidate SET
                        status=:status,revision=:revision,payload=CAST(:payload AS jsonb),
                        updated_at=now()
                    WHERE run_id=:run_id AND candidate_id=:candidate_id
                      AND revision=:expected_revision
                """),
                {
                    "run_id": candidate.run_id,
                    "candidate_id": candidate.candidate_id,
                    "status": candidate.status,
                    "revision": candidate.revision,
                    "payload": self._payload(candidate),
                    "expected_revision": expected_revision,
                },
            )
            if updated.rowcount != 1:
                raise OntologyConflictError("Construction candidate revision conflict")
            connection.execute(
                text("""
                    INSERT INTO ontology_construction_candidate_review(
                        review_id,run_id,candidate_id,revision,reviewer,decision,payload
                    ) VALUES (
                        :review_id,:run_id,:candidate_id,:revision,:reviewer,:decision,
                        CAST(:payload AS jsonb)
                    )
                """),
                {
                    "review_id": (
                        f"review-{candidate.run_id}-{candidate.candidate_id}-"
                        f"{candidate.revision}"
                    )[:160],
                    "run_id": candidate.run_id,
                    "candidate_id": candidate.candidate_id,
                    "revision": candidate.revision,
                    "reviewer": candidate.reviewer,
                    "decision": candidate.decision,
                    "payload": self._payload(candidate),
                },
            )
