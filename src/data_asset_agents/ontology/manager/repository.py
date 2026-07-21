"""Persistence for isolated drafts and atomically published object resources."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import Engine, text

from data_asset_agents.core.errors import (
    DataAssetAgentsError,
    OntologyConflictError,
    OntologyError,
    OntologyGovernanceError,
)
from data_asset_agents.ontology.models import OntologyBundle, OntologyVersion
from data_asset_agents.ontology.repository.postgres_repository import PostgresOntologyRepository

from .hashing import calculate_draft_resource_hash
from .models import (
    CompiledOntologyArtifact,
    DataSourceDefinition,
    DimensionDefinition,
    DraftResources,
    LinkType,
    MetricDefinition,
    ObjectDataSourceBinding,
    ObjectType,
    OntologyAuditAction,
    OntologyAuditEvent,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PropertyDefinition,
    ValidationState,
)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)


class OntologyManagerRepository(Protocol):
    def save_data_source(self, source: DataSourceDefinition) -> None: ...
    def list_data_sources(self) -> list[DataSourceDefinition]: ...
    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate: ...
    def list_drafts(self) -> list[OntologyDraft]: ...
    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None: ...
    def save_draft(self, draft: OntologyDraft) -> None: ...
    def delete_draft(self, draft_id: str, expected_revision: int | None = None) -> None: ...
    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]: ...
    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
        artifact: CompiledOntologyArtifact,
    ) -> None: ...
    def apply_mutation(
        self,
        draft_id: str,
        expected_revision: int | None,
        actor: str,
        action: OntologyAuditAction,
        resource_type: str | None,
        resource_id: str | None,
        callback: Callable[[DraftResources], None],
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate: ...
    def update_draft_state(
        self,
        draft: OntologyDraft,
        expected_revision: int,
        expected_hash: str,
        actor: str,
        action: OntologyAuditAction,
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate: ...
    def list_audit_events(
        self, *, draft_id: str | None = None, version_id: str | None = None, limit: int = 100
    ) -> list[OntologyAuditEvent]: ...
    def append_audit_event(self, event: OntologyAuditEvent) -> None: ...
    def get_compiled_artifact(self, version_id: str) -> CompiledOntologyArtifact | None: ...


RESOURCE_TABLES = {
    ObjectType: ("draft_object_type", "object_type_id"),
    PropertyDefinition: ("draft_property_definition", "property_id"),
    LinkType: ("draft_link_type", "link_type_id"),
    ObjectDataSourceBinding: ("draft_object_data_source_binding", "binding_id"),
    PhysicalJoinDefinition: ("draft_physical_join", "physical_join_id"),
    MetricDefinition: ("draft_metric_definition", "metric_id"),
    DimensionDefinition: ("draft_dimension_definition", "dimension_id"),
}
KIND_TABLES = {
    "object_type": ("draft_object_type", "object_type_id"),
    "property": ("draft_property_definition", "property_id"),
    "link_type": ("draft_link_type", "link_type_id"),
    "binding": ("draft_object_data_source_binding", "binding_id"),
    "physical_join": ("draft_physical_join", "physical_join_id"),
    "metric": ("draft_metric_definition", "metric_id"),
    "dimension": ("draft_dimension_definition", "dimension_id"),
}


def _revision_conflict(draft: OntologyDraft) -> OntologyConflictError:
    return OntologyConflictError(
        "Draft 已被其他操作更新，请刷新后重新编辑。",
        "DRAFT_REVISION_CONFLICT",
        current_revision=draft.resource_revision,
        current_hash=draft.resource_hash,
    )


def _safe_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
    blocked = (
        "password",
        "secret",
        "api_key",
        "connection",
        "database_url",
        "credential",
        "token",
        "dsn",
    )

    def sanitize(value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if not any(token in str(key).lower() for token in blocked)
            }
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in value]
        if isinstance(value, str) and "://" in value:
            return "[REDACTED]"
        return value

    return sanitize(metadata or {})  # type: ignore[return-value]


def _event(
    *,
    draft: OntologyDraft | None,
    actor: str,
    action: OntologyAuditAction,
    resource_type: str | None = None,
    resource_id: str | None = None,
    before_revision: int | None = None,
    before_hash: str | None = None,
    version_id: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OntologyAuditEvent:
    return OntologyAuditEvent(
        event_id=f"audit-{uuid4().hex}",
        draft_id=draft.id if draft else None,
        ontology_version_id=version_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before_revision=before_revision,
        after_revision=draft.resource_revision if draft else None,
        before_hash=before_hash,
        after_hash=draft.resource_hash if draft else None,
        request_id=request_id,
        metadata=_safe_metadata(metadata),
    )


class MemoryOntologyManagerRepository:
    """Small deterministic repository for state-machine and isolation tests."""

    def __init__(self) -> None:
        self.drafts: dict[str, OntologyDraftAggregate] = {}
        self.sources: dict[str, DataSourceDefinition] = {}
        self.current: tuple[str | None, DraftResources] = (None, DraftResources())
        self.versions: dict[str, DraftResources] = {}
        self.artifacts: dict[str, CompiledOntologyArtifact] = {}
        self.audit_events: list[OntologyAuditEvent] = []
        self._lock = threading.RLock()

    def save_data_source(self, source: DataSourceDefinition) -> None:
        self.sources[source.id] = deepcopy(source)

    def list_data_sources(self) -> list[DataSourceDefinition]:
        return list(deepcopy(self.sources).values())

    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate:
        with self._lock:
            if draft.id in self.drafts:
                raise OntologyError(f"Draft already exists: {draft.id}")
            resources = resources or DraftResources()
            draft.resource_hash = calculate_draft_resource_hash(resources)
            aggregate = OntologyDraftAggregate(draft=draft, resources=resources)
            self.drafts[draft.id] = deepcopy(aggregate)
            self.audit_events.append(
                _event(
                    draft=draft,
                    actor=draft.created_by,
                    action=OntologyAuditAction.DRAFT_CREATED,
                    before_revision=None,
                    before_hash=None,
                )
            )
            return deepcopy(aggregate)

    def list_drafts(self) -> list[OntologyDraft]:
        return [deepcopy(item.draft) for item in self.drafts.values()]

    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None:
        item = self.drafts.get(draft_id)
        return deepcopy(item) if item else None

    def save_draft(self, draft: OntologyDraft) -> None:
        self.drafts[draft.id].draft = deepcopy(draft)

    def delete_draft(self, draft_id: str, expected_revision: int | None = None) -> None:
        with self._lock:
            aggregate = self.drafts.get(draft_id)
            if aggregate is None:
                return
            if (
                expected_revision is not None
                and aggregate.draft.resource_revision != expected_revision
            ):
                raise _revision_conflict(aggregate.draft)
            del self.drafts[draft_id]

    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]:
        if version_id is not None:
            return version_id, deepcopy(self.versions.get(version_id, DraftResources()))
        return deepcopy(self.current)

    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
        artifact: CompiledOntologyArtifact,
    ) -> None:
        with self._lock:
            if version.id in self.artifacts:
                raise OntologyError(f"Compiled artifact already exists: {version.id}")
            self.current = (version.id, deepcopy(resources))
            self.versions[version.id] = deepcopy(resources)
            self.artifacts[version.id] = deepcopy(artifact)
            self.drafts[draft.id] = OntologyDraftAggregate(
                draft=deepcopy(draft), resources=deepcopy(resources)
            )
            self.audit_events.append(
                _event(
                    draft=draft,
                    actor=version.published_by,
                    action=OntologyAuditAction.PUBLISHED,
                    version_id=version.id,
                    before_revision=draft.resource_revision,
                    before_hash=draft.resource_hash,
                    metadata={"bundle_hash": artifact.bundle_hash},
                )
            )

    def apply_mutation(
        self,
        draft_id: str,
        expected_revision: int | None,
        actor: str,
        action: OntologyAuditAction,
        resource_type: str | None,
        resource_id: str | None,
        callback: Callable[[DraftResources], None],
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate:
        with self._lock:
            current = self.drafts.get(draft_id)
            if current is None:
                raise OntologyError(f"Ontology Draft not found: {draft_id}")
            if current.draft.status.value != "DRAFT":
                raise OntologyConflictError(
                    f"Draft {draft_id} is {current.draft.status}; only DRAFT resources are editable"
                )
            if (
                expected_revision is not None
                and current.draft.resource_revision != expected_revision
            ):
                raise _revision_conflict(current.draft)
            working = deepcopy(current)
            before_revision = working.draft.resource_revision
            before_hash = working.draft.resource_hash
            callback(working.resources)
            after_hash = calculate_draft_resource_hash(working.resources)
            if after_hash == before_hash:
                return deepcopy(current)
            working.draft.resource_revision += 1
            working.draft.resource_hash = after_hash
            working.draft.validated_revision = None
            working.draft.validated_hash = None
            working.draft.submitted_revision = None
            working.draft.submitted_hash = None
            working.draft.validation_report = None
            working.draft.validation_state = ValidationState.STALE
            from datetime import UTC, datetime

            working.draft.updated_at = datetime.now(UTC)
            self.drafts[draft_id] = deepcopy(working)
            self.audit_events.append(
                _event(
                    draft=working.draft,
                    actor=actor,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    before_revision=before_revision,
                    before_hash=before_hash,
                    request_id=request_id,
                    metadata=metadata,
                )
            )
            return deepcopy(working)

    def update_draft_state(
        self,
        draft: OntologyDraft,
        expected_revision: int,
        expected_hash: str,
        actor: str,
        action: OntologyAuditAction,
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate:
        with self._lock:
            current = self.drafts.get(draft.id)
            if current is None:
                raise OntologyError(f"Ontology Draft not found: {draft.id}")
            if current.draft.resource_revision != expected_revision:
                if action in {
                    OntologyAuditAction.VALIDATION_PASSED,
                    OntologyAuditAction.VALIDATION_FAILED,
                }:
                    raise OntologyGovernanceError(
                        "Draft changed during validation",
                        "DRAFT_CHANGED_DURING_VALIDATION",
                        current_revision=current.draft.resource_revision,
                        current_hash=current.draft.resource_hash,
                    )
                raise _revision_conflict(current.draft)
            if current.draft.resource_hash != expected_hash:
                raise OntologyGovernanceError(
                    "Draft content changed during operation",
                    "DRAFT_CHANGED_DURING_VALIDATION",
                    current_revision=current.draft.resource_revision,
                    current_hash=current.draft.resource_hash,
                )
            current.draft = deepcopy(draft)
            self.drafts[draft.id] = deepcopy(current)
            self.audit_events.append(
                _event(
                    draft=draft,
                    actor=actor,
                    action=action,
                    before_revision=expected_revision,
                    before_hash=expected_hash,
                    request_id=request_id,
                    metadata=metadata,
                )
            )
            return deepcopy(current)

    def append_audit_event(self, event: OntologyAuditEvent) -> None:
        with self._lock:
            safe = event.model_copy(update={"metadata": _safe_metadata(event.metadata)})
            self.audit_events.append(deepcopy(safe))

    def list_audit_events(
        self,
        *,
        draft_id: str | None = None,
        version_id: str | None = None,
        limit: int = 100,
    ) -> list[OntologyAuditEvent]:
        events = [
            event
            for event in self.audit_events
            if (draft_id is None or event.draft_id == draft_id)
            and (version_id is None or event.ontology_version_id == version_id)
        ]
        return deepcopy(sorted(events, key=lambda item: item.created_at, reverse=True)[:limit])

    def get_compiled_artifact(self, version_id: str) -> CompiledOntologyArtifact | None:
        artifact = self.artifacts.get(version_id)
        return deepcopy(artifact) if artifact else None


class PostgresOntologyManagerRepository:
    """PostgreSQL implementation with one publication transaction."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._migrate_existing_draft_hashes()

    def _migrate_existing_draft_hashes(self) -> None:
        """Recalculate pre-V2 Draft identities after DDL 009 is installed."""

        with self.engine.begin() as connection:
            draft_ids = list(
                connection.execute(
                    text("SELECT draft_id FROM ontology_draft ORDER BY draft_id")
                ).scalars()
            )
            for draft_id in draft_ids:
                aggregate = self._load_draft(connection, str(draft_id), for_update=True)
                if aggregate is None:
                    continue
                calculated = calculate_draft_resource_hash(aggregate.resources)
                if aggregate.draft.resource_hash == calculated:
                    continue
                aggregate.draft.resource_hash = calculated
                aggregate.draft.validation_report = None
                aggregate.draft.validated_revision = None
                aggregate.draft.validated_hash = None
                aggregate.draft.submitted_revision = None
                aggregate.draft.submitted_hash = None
                aggregate.draft.validation_state = (
                    ValidationState.STALE
                    if any(
                        getattr(aggregate.resources, name)
                        for name in type(aggregate.resources).model_fields
                    )
                    else ValidationState.NEVER_VALIDATED
                )
                self._update_draft_row(connection, aggregate.draft)

    def save_data_source(self, source: DataSourceDefinition) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_source_definition(
                  data_source_id, provider, connection_ref, payload
                )
                VALUES (:id, :provider, :connection_ref, CAST(:payload AS jsonb))
                ON CONFLICT (data_source_id) DO UPDATE SET
                  provider=EXCLUDED.provider, connection_ref=EXCLUDED.connection_ref,
                  payload=EXCLUDED.payload
            """),
                {
                    "id": source.id,
                    "provider": source.provider,
                    "connection_ref": source.connection_ref,
                    "payload": _json(source),
                },
            )

    def list_data_sources(self) -> list[DataSourceDefinition]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT payload FROM data_source_definition ORDER BY data_source_id")
            ).scalars()
            return [DataSourceDefinition.model_validate(row) for row in rows]

    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate:
        resources = resources or DraftResources()
        draft.resource_hash = calculate_draft_resource_hash(resources)
        with self.engine.begin() as connection:
            self._insert_draft(connection, draft)
            self._insert_resources(connection, draft.id, resources)
            self._insert_audit(
                connection,
                _event(
                    draft=draft,
                    actor=draft.created_by,
                    action=OntologyAuditAction.DRAFT_CREATED,
                ),
            )
        return self.get_draft(draft.id) or OntologyDraftAggregate(draft=draft)

    @staticmethod
    def _insert_draft(connection: Any, draft: OntologyDraft) -> None:
        values = draft.model_dump(mode="python")
        values["validation_report"] = (
            _json(draft.validation_report) if draft.validation_report else None
        )
        connection.execute(
            text("""
            INSERT INTO ontology_draft
              (draft_id,name,description,base_version_id,source_snapshot_id,status,created_by,
               submitted_by,reviewed_by,created_at,updated_at,submitted_at,reviewed_at,
               validation_report,rejection_reason,resource_revision,resource_hash,
               validated_revision,validated_hash,submitted_revision,submitted_hash,
               validation_state)
            VALUES (:id,:name,:description,:base_version_id,:source_snapshot_id,:status,:created_by,
               :submitted_by,:reviewed_by,:created_at,:updated_at,:submitted_at,:reviewed_at,
               CAST(:validation_report AS jsonb),:rejection_reason,
               :resource_revision,:resource_hash,
               :validated_revision,:validated_hash,:submitted_revision,:submitted_hash,
               :validation_state)
        """),
            values,
        )

    @staticmethod
    def _insert_resources(connection: Any, draft_id: str, resources: DraftResources) -> None:
        groups: list[Iterable[Any]] = [
            resources.object_types,
            resources.properties,
            resources.link_types,
            resources.bindings,
            resources.physical_joins,
            resources.metrics,
            resources.dimensions,
        ]
        for group in groups:
            for resource in group:
                table, key = RESOURCE_TABLES[type(resource)]
                values = {"draft_id": draft_id, "key": resource.id, "payload": _json(resource)}
                if isinstance(resource, ObjectDataSourceBinding):
                    connection.execute(
                        text(
                            f"INSERT INTO {table} "
                            f"(draft_id,{key},data_source_id,payload) "
                            "VALUES (:draft_id,:key,:data_source_id,"
                            "CAST(:payload AS jsonb))"
                        ),
                        {**values, "data_source_id": resource.data_source_id},
                    )
                else:
                    connection.execute(
                        text(
                            f"INSERT INTO {table} (draft_id,{key},payload) "
                            "VALUES (:draft_id,:key,CAST(:payload AS jsonb))"
                        ),
                        values,
                    )

    def list_drafts(self) -> list[OntologyDraft]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT * FROM ontology_draft ORDER BY updated_at DESC")
            ).mappings()
            return [self._draft(row) for row in rows]

    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None:
        with self.engine.connect() as connection:
            return self._load_draft(connection, draft_id)

    def _load_draft(
        self, connection: Any, draft_id: str, *, for_update: bool = False
    ) -> OntologyDraftAggregate | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            connection.execute(
                text(f"SELECT * FROM ontology_draft WHERE draft_id=:id{suffix}"),
                {"id": draft_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return OntologyDraftAggregate(
            draft=self._draft(row),
            resources=DraftResources(
                object_types=self._payloads(connection, "draft_object_type", draft_id, ObjectType),
                properties=self._payloads(
                    connection, "draft_property_definition", draft_id, PropertyDefinition
                ),
                link_types=self._payloads(connection, "draft_link_type", draft_id, LinkType),
                bindings=self._payloads(
                    connection,
                    "draft_object_data_source_binding",
                    draft_id,
                    ObjectDataSourceBinding,
                ),
                physical_joins=self._payloads(
                    connection, "draft_physical_join", draft_id, PhysicalJoinDefinition
                ),
                metrics=self._payloads(
                    connection, "draft_metric_definition", draft_id, MetricDefinition
                ),
                dimensions=self._payloads(
                    connection, "draft_dimension_definition", draft_id, DimensionDefinition
                ),
            ),
        )

    @staticmethod
    def _replace_resources(connection: Any, draft_id: str, resources: DraftResources) -> None:
        for table, _ in KIND_TABLES.values():
            connection.execute(text(f"DELETE FROM {table} WHERE draft_id=:id"), {"id": draft_id})
        PostgresOntologyManagerRepository._insert_resources(connection, draft_id, resources)

    @staticmethod
    def _insert_audit(connection: Any, event: OntologyAuditEvent) -> None:
        connection.execute(
            text("""
            INSERT INTO ontology_audit_event(
              event_id,draft_id,ontology_version_id,actor,action,resource_type,resource_id,
              before_revision,after_revision,before_hash,after_hash,request_id,created_at,metadata
            ) VALUES (
              :event_id,:draft_id,:ontology_version_id,:actor,:action,:resource_type,:resource_id,
              :before_revision,:after_revision,:before_hash,:after_hash,:request_id,:created_at,
              CAST(:metadata AS jsonb)
            )
            """),
            {**event.model_dump(mode="python"), "metadata": _json(event.metadata)},
        )

    @staticmethod
    def _update_draft_row(connection: Any, draft: OntologyDraft) -> None:
        values = draft.model_dump(mode="python")
        values["validation_report"] = (
            _json(draft.validation_report) if draft.validation_report else None
        )
        connection.execute(
            text("""
            UPDATE ontology_draft SET name=:name,description=:description,status=:status,
              submitted_by=:submitted_by,reviewed_by=:reviewed_by,updated_at=:updated_at,
              submitted_at=:submitted_at,reviewed_at=:reviewed_at,
              validation_report=CAST(:validation_report AS jsonb),
              rejection_reason=:rejection_reason,resource_revision=:resource_revision,
              resource_hash=:resource_hash,validated_revision=:validated_revision,
              validated_hash=:validated_hash,submitted_revision=:submitted_revision,
              submitted_hash=:submitted_hash,validation_state=:validation_state
            WHERE draft_id=:id
            """),
            values,
        )

    @staticmethod
    def _payloads(connection: Any, table: str, draft_id: str, model: Any) -> list[Any]:
        rows = connection.execute(
            text(f"SELECT payload FROM {table} WHERE draft_id=:id ORDER BY 1"), {"id": draft_id}
        ).scalars()
        return [model.model_validate(row) for row in rows]

    @staticmethod
    def _draft(row: Any) -> OntologyDraft:
        return OntologyDraft(
            id=row["draft_id"],
            name=row["name"],
            description=row["description"],
            base_version_id=row["base_version_id"],
            source_snapshot_id=row["source_snapshot_id"],
            status=row["status"],
            created_by=row["created_by"],
            submitted_by=row["submitted_by"],
            reviewed_by=row["reviewed_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            submitted_at=row["submitted_at"],
            reviewed_at=row["reviewed_at"],
            validation_report=row["validation_report"],
            rejection_reason=row["rejection_reason"],
            resource_revision=row.get("resource_revision", 0),
            resource_hash=row.get("resource_hash", ""),
            validated_revision=row.get("validated_revision"),
            validated_hash=row.get("validated_hash"),
            submitted_revision=row.get("submitted_revision"),
            submitted_hash=row.get("submitted_hash"),
            validation_state=row.get("validation_state", "NEVER_VALIDATED"),
        )

    def save_draft(self, draft: OntologyDraft) -> None:
        values = draft.model_dump(mode="python")
        values["validation_report"] = (
            _json(draft.validation_report) if draft.validation_report else None
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE ontology_draft SET name=:name,description=:description,status=:status,
                  submitted_by=:submitted_by,reviewed_by=:reviewed_by,updated_at=:updated_at,
                  submitted_at=:submitted_at,reviewed_at=:reviewed_at,
                  validation_report=CAST(:validation_report AS jsonb),
                  rejection_reason=:rejection_reason,resource_revision=:resource_revision,
                  resource_hash=:resource_hash,validated_revision=:validated_revision,
                  validated_hash=:validated_hash,submitted_revision=:submitted_revision,
                  submitted_hash=:submitted_hash,validation_state=:validation_state
                WHERE draft_id=:id
            """),
                values,
            )

    def delete_draft(self, draft_id: str, expected_revision: int | None = None) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text("SELECT * FROM ontology_draft WHERE draft_id=:id FOR UPDATE"),
                {"id": draft_id},
            ).mappings().first()
            if row is None:
                return
            current = self._draft_from_row(row)
            if (
                expected_revision is not None
                and current.resource_revision != expected_revision
            ):
                raise _revision_conflict(current)
            connection.execute(
                text("DELETE FROM ontology_draft WHERE draft_id=:id"), {"id": draft_id}
            )

    def apply_mutation(
        self,
        draft_id: str,
        expected_revision: int | None,
        actor: str,
        action: OntologyAuditAction,
        resource_type: str | None,
        resource_id: str | None,
        callback: Callable[[DraftResources], None],
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate:
        with self.engine.begin() as connection:
            current = self._load_draft(connection, draft_id, for_update=True)
            if current is None:
                raise OntologyError(f"Ontology Draft not found: {draft_id}")
            if current.draft.status.value != "DRAFT":
                raise OntologyConflictError(
                    f"Draft {draft_id} is {current.draft.status}; only DRAFT resources are editable"
                )
            if (
                expected_revision is not None
                and current.draft.resource_revision != expected_revision
            ):
                raise _revision_conflict(current.draft)
            before_revision = current.draft.resource_revision
            before_hash = current.draft.resource_hash
            callback(current.resources)
            after_hash = calculate_draft_resource_hash(current.resources)
            if after_hash == before_hash:
                return current
            current.draft.resource_revision += 1
            current.draft.resource_hash = after_hash
            current.draft.validated_revision = None
            current.draft.validated_hash = None
            current.draft.submitted_revision = None
            current.draft.submitted_hash = None
            current.draft.validation_report = None
            current.draft.validation_state = ValidationState.STALE
            current.draft.updated_at = datetime.now(UTC)
            self._replace_resources(connection, draft_id, current.resources)
            self._update_draft_row(connection, current.draft)
            self._insert_audit(
                connection,
                _event(
                    draft=current.draft,
                    actor=actor,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    before_revision=before_revision,
                    before_hash=before_hash,
                    request_id=request_id,
                    metadata=metadata,
                ),
            )
            return current

    def update_draft_state(
        self,
        draft: OntologyDraft,
        expected_revision: int,
        expected_hash: str,
        actor: str,
        action: OntologyAuditAction,
        *,
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> OntologyDraftAggregate:
        with self.engine.begin() as connection:
            current = self._load_draft(connection, draft.id, for_update=True)
            if current is None:
                raise OntologyError(f"Ontology Draft not found: {draft.id}")
            if current.draft.resource_revision != expected_revision:
                if action in {
                    OntologyAuditAction.VALIDATION_PASSED,
                    OntologyAuditAction.VALIDATION_FAILED,
                }:
                    raise OntologyGovernanceError(
                        "Draft changed during validation",
                        "DRAFT_CHANGED_DURING_VALIDATION",
                        current_revision=current.draft.resource_revision,
                        current_hash=current.draft.resource_hash,
                    )
                raise _revision_conflict(current.draft)
            if current.draft.resource_hash != expected_hash:
                raise OntologyGovernanceError(
                    "Draft content changed during operation",
                    "DRAFT_CHANGED_DURING_VALIDATION",
                    current_revision=current.draft.resource_revision,
                    current_hash=current.draft.resource_hash,
                )
            self._update_draft_row(connection, draft)
            self._insert_audit(
                connection,
                _event(
                    draft=draft,
                    actor=actor,
                    action=action,
                    before_revision=expected_revision,
                    before_hash=expected_hash,
                    request_id=request_id,
                    metadata=metadata,
                ),
            )
        return self.get_draft(draft.id) or OntologyDraftAggregate(draft=draft)

    def append_audit_event(self, event: OntologyAuditEvent) -> None:
        with self.engine.begin() as connection:
            safe = event.model_copy(update={"metadata": _safe_metadata(event.metadata)})
            self._insert_audit(connection, safe)

    def list_audit_events(
        self,
        *,
        draft_id: str | None = None,
        version_id: str | None = None,
        limit: int = 100,
    ) -> list[OntologyAuditEvent]:
        filters: list[str] = []
        params: dict[str, object] = {"limit": min(max(limit, 1), 500)}
        if draft_id is not None:
            filters.append("draft_id=:draft_id")
            params["draft_id"] = draft_id
        if version_id is not None:
            filters.append("ontology_version_id=:version_id")
            params["version_id"] = version_id
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ontology_audit_event"
                    + where
                    + " ORDER BY created_at DESC,event_id DESC LIMIT :limit"
                ),
                params,
            ).mappings()
            return [OntologyAuditEvent.model_validate(dict(row)) for row in rows]

    def get_compiled_artifact(self, version_id: str) -> CompiledOntologyArtifact | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ontology_compiled_artifact "
                    "WHERE ontology_version_id=:id "
                    "ORDER BY CASE status WHEN 'READY' THEN 0 ELSE 1 END, "
                    "created_at DESC, artifact_id DESC LIMIT 1"
                ),
                {"id": version_id},
            ).mappings().first()
        if row is None:
            return None
        values = dict(row)
        return CompiledOntologyArtifact.model_validate(values)

    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]:
        with self.engine.connect() as connection:
            target = (
                version_id
                or connection.execute(
                    text(
                        "SELECT version_id FROM ontology_version "
                        "WHERE is_current AND status='PUBLISHED' LIMIT 1"
                    )
                ).scalar_one_or_none()
            )
            if target is None:
                return None, DraftResources()

            def rows(table: str, model: Any) -> list[Any]:
                payloads = connection.execute(
                    text(f"SELECT payload FROM {table} WHERE ontology_version_id=:id"),
                    {"id": target},
                ).scalars()
                return [model.model_validate(item) for item in payloads]

            return target, DraftResources(
                object_types=rows("published_object_type", ObjectType),
                properties=rows("published_property_definition", PropertyDefinition),
                link_types=rows("published_link_type", LinkType),
                bindings=rows("published_object_data_source_binding", ObjectDataSourceBinding),
                physical_joins=rows("published_physical_join", PhysicalJoinDefinition),
                metrics=rows("published_metric_definition", MetricDefinition),
                dimensions=rows("published_dimension_definition", DimensionDefinition),
            )

    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
        artifact: CompiledOntologyArtifact,
    ) -> None:
        try:
            with self.engine.begin() as connection:
                current = self._load_draft(connection, draft.id, for_update=True)
                if current is None:
                    raise OntologyError(f"Ontology Draft not found: {draft.id}")
                if (
                    current.draft.resource_revision != draft.resource_revision
                    or current.draft.resource_hash != draft.resource_hash
                ):
                    raise _revision_conflict(current.draft)
                connection.execute(
                    text("UPDATE ontology_version SET is_current=false WHERE is_current")
                )
                connection.execute(
                    text("""
                    INSERT INTO ontology_version(
                      version_id,version,description,source_snapshot_id,status,
                      concept_count,mapping_count,join_count,published_at,published_by,
                      is_current,bundle_json
                    ) VALUES (
                      :id,:version,:description,:snapshot,'PUBLISHED',
                      :concept_count,:mapping_count,
                      :join_count,:published_at,:published_by,true,CAST(:bundle AS jsonb))
                """),
                    {
                        "id": version.id,
                        "version": version.version,
                        "description": version.description,
                        "snapshot": version.source_snapshot_id,
                        "concept_count": version.concept_count,
                        "mapping_count": version.mapping_count,
                        "join_count": version.join_count,
                        "published_at": version.published_at,
                        "published_by": version.published_by,
                        "bundle": _json(bundle),
                    },
                )
                PostgresOntologyRepository._publish_rows(connection, version.id, bundle, [])
                connection.execute(
                    text("""
                    INSERT INTO ontology_compiled_artifact(
                      artifact_id,ontology_version_id,source_draft_id,source_revision,
                      source_resource_hash,compiler_name,compiler_version,compiler_source_hash,
                      status,bundle_hash,bundle_json,property_bindings,
                      metric_compilation_evidence,dimension_compilation_evidence,
                      join_compilation_evidence,created_at,error_message
                    ) VALUES (
                      :artifact_id,:ontology_version_id,:source_draft_id,:source_revision,
                      :source_resource_hash,:compiler_name,:compiler_version,
                      :compiler_source_hash,:status,:bundle_hash,CAST(:bundle_json AS jsonb),
                      CAST(:property_bindings AS jsonb),CAST(:metric_evidence AS jsonb),
                      CAST(:dimension_evidence AS jsonb),CAST(:join_evidence AS jsonb),
                      :created_at,:error_message
                    )
                    """),
                    {
                        **artifact.model_dump(mode="python"),
                        "status": artifact.status.value,
                        "bundle_json": _json(artifact.bundle_json),
                        "property_bindings": _json(artifact.property_bindings),
                        "metric_evidence": _json(artifact.metric_compilation_evidence),
                        "dimension_evidence": _json(
                            artifact.dimension_compilation_evidence
                        ),
                        "join_evidence": _json(artifact.join_compilation_evidence),
                    },
                )
                groups = [
                    (
                        "published_object_type",
                        "object_type_id",
                        "OBJECT_TYPE",
                        resources.object_types,
                    ),
                    (
                        "published_property_definition",
                        "property_id",
                        "PROPERTY",
                        resources.properties,
                    ),
                    ("published_link_type", "link_type_id", "LINK_TYPE", resources.link_types),
                    (
                        "published_object_data_source_binding",
                        "binding_id",
                        "BINDING",
                        resources.bindings,
                    ),
                    (
                        "published_physical_join",
                        "physical_join_id",
                        "PHYSICAL_JOIN",
                        resources.physical_joins,
                    ),
                    (
                        "published_metric_definition",
                        "metric_id",
                        "METRIC",
                        resources.metrics,
                    ),
                    (
                        "published_dimension_definition",
                        "dimension_id",
                        "DIMENSION",
                        resources.dimensions,
                    ),
                ]
                for table, key, kind, items in groups:
                    for item in items:
                        fields = "ontology_version_id," + key + ",payload"
                        values = ":version_id,:key,CAST(:payload AS jsonb)"
                        params = {"version_id": version.id, "key": item.id, "payload": _json(item)}
                        if isinstance(item, ObjectDataSourceBinding):
                            fields = "ontology_version_id,binding_id,data_source_id,payload"
                            values = ":version_id,:key,:data_source_id,CAST(:payload AS jsonb)"
                            params["data_source_id"] = item.data_source_id
                        connection.execute(
                            text(f"INSERT INTO {table}({fields}) VALUES ({values})"), params
                        )
                        connection.execute(
                            text(
                                "INSERT INTO ontology_version_object_resource("
                                "ontology_version_id,resource_type,resource_id,source_draft_id"
                                ") VALUES (:version_id,:kind,:key,:draft_id)"
                            ),
                            {
                                "version_id": version.id,
                                "kind": kind,
                                "key": item.id,
                                "draft_id": draft.id,
                            },
                        )
                connection.execute(
                    text(
                        "UPDATE ontology_draft SET status='PUBLISHED',"
                        "reviewed_by=:reviewer,reviewed_at=:reviewed_at,"
                        "updated_at=:updated_at WHERE draft_id=:id"
                    ),
                    {
                        "id": draft.id,
                        "reviewer": draft.reviewed_by,
                        "reviewed_at": draft.reviewed_at,
                        "updated_at": draft.updated_at,
                    },
                )
                self._insert_audit(
                    connection,
                    _event(
                        draft=draft,
                        actor=version.published_by,
                        action=OntologyAuditAction.PUBLISHED,
                        version_id=version.id,
                        before_revision=draft.resource_revision,
                        before_hash=draft.resource_hash,
                        metadata={
                            "artifact_id": artifact.artifact_id,
                            "bundle_hash": artifact.bundle_hash,
                            "compiler_version": artifact.compiler_version,
                        },
                    ),
                )
        except DataAssetAgentsError:
            raise
        except Exception as exc:
            raise OntologyError(f"Atomic object ontology publication failed: {exc}") from exc
