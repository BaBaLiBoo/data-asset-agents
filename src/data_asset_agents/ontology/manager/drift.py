"""Read-only metadata drift detection for published object bindings."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, inspect, text

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import OntologyBundle

from .governance_models import (
    BindingInspection,
    DriftSeverity,
    OntologySyncRun,
    SchemaDriftReport,
    SyncRunStatus,
)
from .models import ObjectDataSourceBinding
from .repository import OntologyManagerRepository


class MetadataDriftService:
    """Compare database metadata to an immutable published binding snapshot."""

    def __init__(
        self,
        repository: OntologyManagerRepository,
        bundle: OntologyBundle,
        engine: Engine | None,
        ontology_version_id: Callable[[], str],
    ) -> None:
        self.repository = repository
        self.bundle = bundle
        self.engine = engine
        self.ontology_version_id = ontology_version_id
        self._runs: dict[str, OntologySyncRun] = {}
        self._reports: dict[str, SchemaDriftReport] = {}

    def _inspect(self, binding: ObjectDataSourceBinding) -> BindingInspection:
        if self.engine is None:
            raise OntologyError("Database metadata inspection is unavailable")
        inspector = inspect(self.engine)
        columns = {
            str(item["name"]): str(item["type"]).lower()
            for item in inspector.get_columns(
                binding.table_name, schema=binding.schema_name
            )
        }
        primary_keys = list(
            inspector.get_pk_constraint(
                binding.table_name, schema=binding.schema_name
            ).get("constrained_columns")
            or []
        )
        digest = hashlib.sha256(
            (
                f"{binding.schema_name}.{binding.table_name}:"
                + ",".join(sorted(f"{name}:{kind}" for name, kind in columns.items()))
                + ":pk="
                + ",".join(primary_keys)
            ).encode()
        ).hexdigest()
        return BindingInspection(
            binding_id=binding.id,
            schema_hash=digest,
            columns=columns,
            primary_key_columns=primary_keys,
        )

    def inspect_binding(self, binding_id: str, run_id: str | None = None) -> SchemaDriftReport:
        version_id, resources = self.repository.published_resources()
        binding = next((item for item in resources.bindings if item.id == binding_id), None)
        if version_id is None or binding is None:
            raise OntologyError(f"Published binding not found: {binding_id}")
        current = self._inspect(binding)
        previous_columns = binding.schema_columns or {
            column: current.columns.get(column, "unknown")
            for column in binding.property_bindings.values()
        }
        added = sorted(set(current.columns) - set(previous_columns))
        removed = sorted(set(previous_columns) - set(current.columns))
        changed_types = {
            column: {"before": previous_columns[column], "after": current.columns[column]}
            for column in sorted(set(previous_columns) & set(current.columns))
            if previous_columns[column] != "unknown"
            and previous_columns[column] != current.columns[column]
        }
        previous_pk = binding.primary_key_columns or [binding.primary_key_column]
        primary_key_changed = previous_pk != current.primary_key_columns
        bound_by_column = {
            column: property_id
            for property_id, column in binding.property_bindings.items()
        }
        affected_properties = sorted(
            bound_by_column[column]
            for column in set(removed) | set(changed_types)
            if column in bound_by_column
        )
        endpoint_changes = sorted(
            join.id
            for join in resources.physical_joins
            if (
                (join.left_table == binding.table_name and join.left_column in removed)
                or (join.right_table == binding.table_name and join.right_column in removed)
                or (
                    join.left_table == binding.table_name
                    and join.left_column in changed_types
                )
                or (
                    join.right_table == binding.table_name
                    and join.right_column in changed_types
                )
            )
        )
        affected_links = sorted(
            link.id
            for link in resources.link_types
            if set(link.physical_join_ids) & set(endpoint_changes)
        )
        affected_metrics = sorted(
            metric.id
            for metric in self.bundle.metrics
            if {
                metric.measure_property_id,
                metric.time_property_id,
                *(item.property_id for item in metric.filter_predicates),
            }
            & set(affected_properties)
        )
        breaking = bool(
            affected_properties or primary_key_changed or endpoint_changes
        )
        severity = (
            DriftSeverity.BREAKING
            if breaking
            else DriftSeverity.ADDITIVE
            if added
            else DriftSeverity.NONE
        )
        report = SchemaDriftReport(
            id=f"drift-{uuid4().hex}",
            binding_id=binding.id,
            ontology_version_id=version_id,
            previous_schema_hash=binding.schema_hash,
            current_schema_hash=current.schema_hash,
            added_columns=added,
            removed_columns=removed,
            changed_types=changed_types,
            primary_key_changed=primary_key_changed,
            join_endpoints_changed=endpoint_changes,
            affected_properties=affected_properties,
            affected_links=affected_links,
            affected_metrics=affected_metrics,
            severity=severity,
        )
        self._reports[report.id] = report
        self._persist_report(report, run_id)
        return report

    def create_run(self) -> OntologySyncRun:
        version_id = self.ontology_version_id()
        run = OntologySyncRun(
            run_id=f"sync-{uuid4().hex}", ontology_version_id=version_id
        )
        self._runs[run.run_id] = run
        self._persist_run(run)
        try:
            _, resources = self.repository.published_resources(version_id)
            run.reports = [
                self.inspect_binding(binding.id, run.run_id)
                for binding in resources.bindings
            ]
            run.status = SyncRunStatus.READY
            run.completed_at = datetime.now(UTC)
        except Exception as exc:
            run.status = SyncRunStatus.FAILED
            run.completed_at = datetime.now(UTC)
            run.error_message = str(exc)
        self._runs[run.run_id] = run
        self._persist_run(run)
        return run

    def list_runs(self) -> list[OntologySyncRun]:
        if self.engine is None:
            return sorted(self._runs.values(), key=lambda item: item.started_at, reverse=True)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT payload FROM ontology_sync_run ORDER BY started_at DESC")
            ).scalars()
            return [OntologySyncRun.model_validate(item) for item in rows]

    def get_run(self, run_id: str) -> OntologySyncRun | None:
        return next((item for item in self.list_runs() if item.run_id == run_id), None)

    def list_reports(self) -> list[SchemaDriftReport]:
        if self.engine is None:
            return list(self._reports.values())
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT payload FROM ontology_schema_drift ORDER BY inspected_at DESC")
            ).scalars()
            return [SchemaDriftReport.model_validate(item) for item in rows]

    def has_breaking_drift(self, version_id: str | None) -> bool:
        return any(
            item.ontology_version_id == version_id
            and item.severity == DriftSeverity.BREAKING
            for item in self.list_reports()
        )

    def _persist_run(self, run: OntologySyncRun) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO ontology_sync_run(
                  run_id,ontology_version_id,status,started_at,completed_at,error_message,payload
                ) VALUES (
                  :run_id,:version_id,:status,:started_at,:completed_at,:error_message,
                  CAST(:payload AS jsonb)
                ) ON CONFLICT(run_id) DO UPDATE SET
                  status=EXCLUDED.status,completed_at=EXCLUDED.completed_at,
                  error_message=EXCLUDED.error_message,payload=EXCLUDED.payload
                """),
                {
                    "run_id": run.run_id,
                    "version_id": run.ontology_version_id,
                    "status": run.status.value,
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                    "error_message": run.error_message,
                    "payload": run.model_dump_json(),
                },
            )

    def _persist_report(self, report: SchemaDriftReport, run_id: str | None) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO ontology_schema_drift(
                  report_id,run_id,ontology_version_id,binding_id,severity,inspected_at,payload
                ) VALUES (
                  :id,:run_id,:version_id,:binding_id,:severity,:inspected_at,
                  CAST(:payload AS jsonb)
                )
                """),
                {
                    "id": report.id,
                    "run_id": run_id,
                    "version_id": report.ontology_version_id,
                    "binding_id": report.binding_id,
                    "severity": report.severity.value,
                    "inspected_at": report.inspected_at,
                    "payload": report.model_dump_json(),
                },
            )
