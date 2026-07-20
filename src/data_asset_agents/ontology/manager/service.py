"""Application service for isolated editing, review and atomic publication."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, inspect

from data_asset_agents.core.errors import OntologyConflictError, OntologyError
from data_asset_agents.ontology.models import (
    CandidateConcept,
    OntologyBundle,
    OntologyVersion,
    ReviewStatus,
)

from .change_analysis import OntologyChangeAnalyzer
from .governance_models import OntologyChangeSet, OntologyImpactReport
from .migration import LegacyOntologyObjectMigrator
from .models import (
    ActorRequest,
    BindingSyncStatus,
    CreateDraftRequest,
    DataSourceInspection,
    DraftResources,
    DraftStatus,
    LinkType,
    MigrateLegacyRequest,
    ObjectDataSourceBinding,
    ObjectGraph,
    ObjectType,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    RejectDraftRequest,
    SemanticRole,
)
from .projection import CompatibilityProjectionService
from .repository import OntologyManagerRepository
from .validator import OntologyDraftValidator


class OntologyManagerService:
    """Enforce Draft isolation and the Candidate → Draft → Published boundary."""

    def __init__(
        self,
        repository: OntologyManagerRepository,
        base_bundle: OntologyBundle,
        validator: OntologyDraftValidator,
        *,
        engine: Engine | None = None,
        candidate_repository: object | None = None,
    ) -> None:
        self.repository = repository
        self.base_bundle = base_bundle
        self.validator = validator
        self.engine = engine
        self.candidate_repository = candidate_repository
        self.migrator = LegacyOntologyObjectMigrator(base_bundle)
        self.projection = CompatibilityProjectionService()
        self.change_analyzer = OntologyChangeAnalyzer(repository, base_bundle, engine)
        self.drift_service: object | None = None
        self.repository.save_data_source(self.migrator.data_source())

    def _with_joins(self, aggregate: OntologyDraftAggregate) -> OntologyDraftAggregate:
        """Compatibility wrapper retained for callers; joins now belong to the Draft."""
        return aggregate

    def _refresh_binding_schema(
        self, binding: ObjectDataSourceBinding
    ) -> ObjectDataSourceBinding:
        if self.engine is None:
            return binding
        inspector = inspect(self.engine)
        columns = inspector.get_columns(binding.table_name, schema=binding.schema_name)
        schema_columns = {
            str(column["name"]): str(column["type"]).lower() for column in columns
        }
        primary_keys = list(
            inspector.get_pk_constraint(
                binding.table_name, schema=binding.schema_name
            ).get("constrained_columns")
            or []
        )
        return binding.model_copy(
            update={
                "schema_hash": hashlib.sha256(
                    (
                        f"{binding.schema_name}.{binding.table_name}:"
                        + ",".join(
                            sorted(f"{name}:{kind}" for name, kind in schema_columns.items())
                        )
                        + ":pk="
                        + ",".join(primary_keys)
                    ).encode()
                ).hexdigest(),
                "schema_columns": schema_columns,
                "primary_key_columns": primary_keys,
                "sync_status": BindingSyncStatus.HEALTHY,
                "last_inspected_at": datetime.now(UTC),
                "error_message": None,
            }
        )

    def create_draft(self, request: CreateDraftRequest) -> OntologyDraftAggregate:
        resources = DraftResources()
        if request.base_version_id:
            _, resources = self.repository.published_resources(request.base_version_id)
        draft = OntologyDraft(
            id=f"draft-{uuid4().hex}",
            name=request.name,
            description=request.description,
            base_version_id=request.base_version_id,
            source_snapshot_id=request.source_snapshot_id,
            created_by=request.created_by,
        )
        return self._with_joins(self.repository.create_draft(draft, resources))

    def migrate_legacy(self, request: MigrateLegacyRequest) -> OntologyDraftAggregate:
        resources = self.migrator.migrate(request.source_snapshot_id)
        resources.bindings = [
            self._refresh_binding_schema(binding) for binding in resources.bindings
        ]
        draft = OntologyDraft(
            id=f"draft-legacy-{uuid4().hex}",
            name=request.draft_name,
            description="Idempotent migration from reviewed YAML compatibility models",
            source_snapshot_id=request.source_snapshot_id,
            created_by=request.created_by,
        )
        aggregate = OntologyDraftAggregate(draft=draft, resources=resources)
        return (
            aggregate
            if request.dry_run
            else self._with_joins(self.repository.create_draft(draft, resources))
        )

    def list_drafts(self) -> list[OntologyDraft]:
        return self.repository.list_drafts()

    def get_draft(self, draft_id: str) -> OntologyDraftAggregate:
        aggregate = self.repository.get_draft(draft_id)
        if aggregate is None:
            raise OntologyError(f"Ontology Draft not found: {draft_id}")
        return self._with_joins(aggregate)

    def delete_draft(self, draft_id: str) -> None:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status == DraftStatus.PUBLISHED:
            raise OntologyConflictError("Published Drafts are immutable and cannot be deleted")
        self.repository.delete_draft(draft_id)

    def _editable(self, draft_id: str) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status != DraftStatus.DRAFT:
            raise OntologyConflictError(
                f"Draft {draft_id} is {aggregate.draft.status}; only DRAFT resources are editable"
            )
        return aggregate

    def save_resource(self, draft_id: str, resource: object) -> OntologyDraftAggregate:
        aggregate = self._editable(draft_id)
        resources = aggregate.resources
        if isinstance(resource, PropertyDefinition):
            obj = next(
                (item for item in resources.object_types if item.id == resource.object_type_id),
                None,
            )
            if obj is None:
                raise OntologyError(f"Property object does not exist: {resource.object_type_id}")
            if resource.id not in obj.property_ids:
                self.repository.save_resource(
                    draft_id,
                    obj.model_copy(
                        update={
                            "property_ids": [*obj.property_ids, resource.id],
                            "updated_at": datetime.now(UTC),
                        }
                    ),
                )
        if isinstance(resource, ObjectDataSourceBinding):
            if resource.data_source_id != "minibank-postgres":
                raise OntologyError("Only the application-managed data source may be bound")
            previous = next((item for item in resources.bindings if item.id == resource.id), None)
            if (
                previous
                and previous.object_type_id == resource.object_type_id
                and previous.table_name == resource.table_name
            ):
                resource.property_bindings = {
                    **previous.property_bindings,
                    **resource.property_bindings,
                }
            if self.engine is not None:
                resource = self._refresh_binding_schema(resource)
        self.repository.save_resource(draft_id, resource)
        return self.get_draft(draft_id)

    def delete_resource(self, draft_id: str, kind: str, resource_id: str) -> OntologyDraftAggregate:
        aggregate = self._editable(draft_id)
        if kind == "object_type":
            for prop in aggregate.resources.properties:
                if prop.object_type_id == resource_id:
                    self.repository.delete_resource(draft_id, "property", prop.id)
            for binding in aggregate.resources.bindings:
                if binding.object_type_id == resource_id:
                    self.repository.delete_resource(draft_id, "binding", binding.id)
            for link in aggregate.resources.link_types:
                if resource_id in {
                    link.source_object_type_id,
                    link.target_object_type_id,
                }:
                    self.repository.delete_resource(draft_id, "link_type", link.id)
        if kind == "property":
            for obj in aggregate.resources.object_types:
                if resource_id in obj.property_ids:
                    if resource_id in {obj.primary_key_property_id, obj.title_property_id}:
                        raise OntologyConflictError(
                            "Primary/title properties must be reassigned before deletion"
                        )
                    self.repository.save_resource(
                        draft_id,
                        obj.model_copy(
                            update={
                                "property_ids": [x for x in obj.property_ids if x != resource_id]
                            }
                        ),
                    )
        self.repository.delete_resource(draft_id, kind, resource_id)
        return self.get_draft(draft_id)

    def import_candidates(self, draft_id: str) -> OntologyDraftAggregate:
        """Import stable object candidates while retaining old candidate APIs as evidence."""
        aggregate = self._editable(draft_id)
        migrated = self.migrator.migrate(aggregate.draft.source_snapshot_id)
        existing = {
            ObjectType: {x.id for x in aggregate.resources.object_types},
            PropertyDefinition: {x.id for x in aggregate.resources.properties},
            LinkType: {x.id for x in aggregate.resources.link_types},
            ObjectDataSourceBinding: {x.id for x in aggregate.resources.bindings},
            PhysicalJoinDefinition: {x.id for x in aggregate.resources.physical_joins},
        }
        for group in (
            migrated.object_types,
            migrated.properties,
            migrated.link_types,
            migrated.bindings,
            migrated.physical_joins,
        ):
            for resource in group:
                if resource.id not in existing[type(resource)]:
                    self.repository.save_resource(draft_id, resource)
        if self.candidate_repository is not None:
            verified = self.candidate_repository.list_candidates(
                status=ReviewStatus.VERIFIED,
                snapshot_id=aggregate.draft.source_snapshot_id,
                limit=10_000,
            )
            refreshed = self.get_draft(draft_id)
            binding_objects = {
                binding.table_name: binding.object_type_id
                for binding in refreshed.resources.bindings
            }
            property_ids = {item.id for item in refreshed.resources.properties}
            for envelope in verified:
                if envelope.candidate_type != "concept":
                    continue
                candidate = CandidateConcept.model_validate(envelope.payload)
                object_id = binding_objects.get(candidate.table_name)
                if not object_id:
                    continue
                property_id = f"{object_id}.{candidate.column_name}"
                if property_id in property_ids:
                    continue
                role = {
                    "identifier": SemanticRole.IDENTIFIER,
                    "measure": SemanticRole.MEASURE,
                    "dimension": SemanticRole.DIMENSION,
                    "time": SemanticRole.TIME,
                    "status": SemanticRole.STATUS,
                    "attribute": SemanticRole.ATTRIBUTE,
                }[candidate.role]
                data_type = (
                    PropertyDataType.DECIMAL
                    if role == SemanticRole.MEASURE
                    else PropertyDataType.DATE
                    if role == SemanticRole.TIME
                    else PropertyDataType.STRING
                )
                self.save_resource(
                    draft_id,
                    PropertyDefinition(
                        id=property_id,
                        object_type_id=object_id,
                        name=candidate.business_name,
                        description=candidate.semantic_property,
                        data_type=data_type,
                        semantic_role=role,
                        nullable=role != SemanticRole.IDENTIFIER,
                        groupable=role in {SemanticRole.DIMENSION, SemanticRole.STATUS},
                        unit=candidate.unit,
                        synonyms=candidate.synonyms,
                    ),
                )
                property_ids.add(property_id)
        return self.get_draft(draft_id)

    def validate(self, draft_id: str) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status in {DraftStatus.PUBLISHED, DraftStatus.REJECTED}:
            raise OntologyConflictError(f"Cannot validate a {aggregate.draft.status} Draft")
        report = self.validator.validate(aggregate.resources, aggregate.draft.source_snapshot_id)
        aggregate.draft.validation_report = report
        aggregate.draft.updated_at = datetime.now(UTC)
        self.repository.save_draft(aggregate.draft)
        return self.get_draft(draft_id)

    def submit(self, draft_id: str, request: ActorRequest) -> OntologyDraftAggregate:
        aggregate = self._editable(draft_id)
        aggregate.draft.status = DraftStatus.IN_REVIEW
        aggregate.draft.submitted_by = request.actor
        aggregate.draft.submitted_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.submitted_at
        self.repository.save_draft(aggregate.draft)
        return self.get_draft(draft_id)

    def approve(self, draft_id: str, request: ActorRequest) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status != DraftStatus.IN_REVIEW:
            raise OntologyConflictError("Only IN_REVIEW Drafts can be approved")
        report = self.validator.validate(aggregate.resources, aggregate.draft.source_snapshot_id)
        if not report.valid:
            aggregate.draft.validation_report = report
            self.repository.save_draft(aggregate.draft)
            raise OntologyError("Draft approval failed validation")
        aggregate.draft.status = DraftStatus.VALIDATED
        aggregate.draft.validation_report = report
        aggregate.draft.reviewed_by = request.actor
        aggregate.draft.reviewed_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.reviewed_at
        self.repository.save_draft(aggregate.draft)
        return self.get_draft(draft_id)

    def reject(self, draft_id: str, request: RejectDraftRequest) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status != DraftStatus.IN_REVIEW:
            raise OntologyConflictError("Only IN_REVIEW Drafts can be rejected")
        aggregate.draft.status = DraftStatus.REJECTED
        aggregate.draft.reviewed_by = request.actor
        aggregate.draft.rejection_reason = request.reason
        aggregate.draft.reviewed_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.reviewed_at
        self.repository.save_draft(aggregate.draft)
        return self.get_draft(draft_id)

    def publish(self, draft_id: str, request: PublishDraftRequest) -> OntologyVersion:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status != DraftStatus.VALIDATED:
            raise OntologyConflictError("Only a VALIDATED Draft can be published")
        impact = self.impact(draft_id)
        if impact.breaking_changes and not request.acknowledge_breaking_changes:
            raise OntologyConflictError(
                "Breaking changes require acknowledge_breaking_changes=true"
            )
        if impact.breaking_changes and not request.change_ticket:
            raise OntologyConflictError("Breaking changes require a change_ticket")
        if (
            self.drift_service is not None
            and aggregate.draft.base_version_id
            and self.drift_service.has_breaking_drift(aggregate.draft.base_version_id)
        ):
            raise OntologyError("Breaking metadata drift blocks publication")
        report = self.validator.validate(aggregate.resources, aggregate.draft.source_snapshot_id)
        if not report.valid:
            raise OntologyError("Draft Dry Run failed; publication is blocked")
        bundle = self.projection.project(self.base_bundle, aggregate.resources)
        bundle.domain["version"] = request.version
        version = OntologyVersion(
            version=request.version,
            description=request.description,
            source_snapshot_id=aggregate.draft.source_snapshot_id,
            concept_count=len(bundle.concepts) + len(bundle.metrics) + len(bundle.dimensions),
            mapping_count=len(bundle.mappings),
            join_count=len(bundle.joins),
            published_by=request.actor,
        )
        aggregate.draft.status = DraftStatus.PUBLISHED
        aggregate.draft.reviewed_by = request.actor
        aggregate.draft.reviewed_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.reviewed_at
        aggregate.draft.validation_report = report
        self.repository.publish(aggregate.draft, aggregate.resources, version, bundle)
        return version

    def diff(self, draft_id: str) -> OntologyChangeSet:
        return self.change_analyzer.diff(self.get_draft(draft_id))

    def impact(self, draft_id: str) -> OntologyImpactReport:
        aggregate = self.get_draft(draft_id)
        return self.change_analyzer.impact(aggregate, self.change_analyzer.diff(aggregate))

    def inspect_data_source(self, data_source_id: str) -> DataSourceInspection:
        sources = {item.id: item for item in self.repository.list_data_sources()}
        source = sources.get(data_source_id)
        if source is None or data_source_id != "minibank-postgres":
            raise OntologyError("Only the application-managed MiniBank data source is allowed")
        if self.engine is None:
            return DataSourceInspection(
                data_source_id=data_source_id,
                healthy=False,
                provider=source.provider,
                schemas=[],
                table_count=0,
                error="Database engine unavailable",
            )
        try:
            inspector = inspect(self.engine)
            tables = inspector.get_table_names(schema="public")
            return DataSourceInspection(
                data_source_id=data_source_id,
                healthy=True,
                provider=source.provider,
                schemas=["public"],
                table_count=len(tables),
            )
        except Exception:
            return DataSourceInspection(
                data_source_id=data_source_id,
                healthy=False,
                provider=source.provider,
                schemas=[],
                table_count=0,
                error="Data source inspection failed",
            )

    def published(self) -> tuple[str | None, DraftResources]:
        return self.repository.published_resources()

    def object_graph(self) -> ObjectGraph:
        version_id, resources = self.published()
        return ObjectGraph(
            version_id=version_id,
            nodes=[
                {"id": item.id, "label": item.name, "status": item.lifecycle_status}
                for item in resources.object_types
            ],
            edges=[
                {
                    "id": item.id,
                    "source": item.source_object_type_id,
                    "target": item.target_object_type_id,
                    "label": item.name,
                }
                for item in resources.link_types
            ],
        )
