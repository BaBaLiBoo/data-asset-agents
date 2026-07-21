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

from .candidate_generator import ObjectFirstCandidateGenerator
from .change_analysis import OntologyChangeAnalyzer
from .governance_models import OntologyChangeSet, OntologyImpactReport
from .migration import LegacyOntologyObjectMigrator
from .models import (
    ActorRequest,
    BindingSyncStatus,
    CreateDraftFromSeedRequest,
    CreateDraftRequest,
    DataSourceDefinition,
    DataSourceInspection,
    DraftResources,
    DraftStatus,
    ImportObjectCandidatesRequest,
    LinkType,
    MigrateLegacyRequest,
    ObjectCandidateSet,
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
from .seed_repository import ObjectOntologySeedRepository
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
        seed_repository: ObjectOntologySeedRepository | None = None,
        candidate_generator: ObjectFirstCandidateGenerator | None = None,
    ) -> None:
        self.repository = repository
        self.base_bundle = base_bundle
        self.validator = validator
        self.engine = engine
        self.candidate_repository = candidate_repository
        self.seed_repository = seed_repository
        self.candidate_generator = candidate_generator
        self.migrator = LegacyOntologyObjectMigrator(base_bundle)
        self.projection = CompatibilityProjectionService()
        self.change_analyzer = OntologyChangeAnalyzer(repository, base_bundle, engine)
        self.drift_service: object | None = None
        self.repository.save_data_source(
            DataSourceDefinition(
                id="minibank-postgres",
                name="MiniBank PostgreSQL",
                connection_ref="DATABASE_URL",
                description="Application-managed fictional MiniBank data source",
            )
        )

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

    def create_draft_from_seed(
        self, request: CreateDraftFromSeedRequest
    ) -> OntologyDraftAggregate:
        """Create an editable Draft directly from an allowlisted object seed."""

        if self.seed_repository is None:
            raise OntologyError("Object ontology seed repository is not configured")
        resources = self.seed_repository.load(request.seed_name)
        refreshed: list[ObjectDataSourceBinding] = []
        for binding in resources.bindings:
            binding = binding.model_copy(
                update={"latest_snapshot_id": request.source_snapshot_id}
            )
            refreshed.append(self._refresh_binding_schema(binding))
        resources.bindings = refreshed
        draft = OntologyDraft(
            id=f"draft-seed-{uuid4().hex}",
            name=request.draft_name,
            description=f"Direct object-first seed: {request.seed_name}",
            source_snapshot_id=request.source_snapshot_id,
            created_by=request.created_by,
        )
        return self.repository.create_draft(draft, resources)

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
            if previous:
                if (
                    previous.object_type_id != resource.object_type_id
                    or previous.table_name != resource.table_name
                ):
                    raise OntologyConflictError(
                        f"Binding {resource.id} conflicts with the manually selected object/table"
                    )
                conflicts = {
                    property_id
                    for property_id, column in resource.property_bindings.items()
                    if property_id in previous.property_bindings
                    and previous.property_bindings[property_id] != column
                }
                if conflicts:
                    raise OntologyConflictError(
                        "Binding candidate conflicts with reviewed property binding(s): "
                        + ", ".join(sorted(conflicts))
                    )
                resource = resource.model_copy(
                    update={
                        "property_bindings": {
                            **previous.property_bindings,
                            **resource.property_bindings,
                        }
                    }
                )
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

    def generate_candidates(self, draft_id: str) -> ObjectCandidateSet:
        """Generate evidence-backed candidates for one Draft's immutable snapshot."""

        aggregate = self._editable(draft_id)
        if not aggregate.draft.source_snapshot_id:
            raise OntologyError("Draft source_snapshot_id is required for candidate generation")
        if self.candidate_repository is None or self.candidate_generator is None:
            raise OntologyError("Metadata object candidate generation is not configured")
        snapshot = self.candidate_repository.get_metadata_snapshot(
            aggregate.draft.source_snapshot_id
        )
        historical_sql = self.candidate_repository.list_historical_sql(snapshot.id)
        return self.candidate_generator.generate(
            snapshot, historical_sql, aggregate.resources
        )

    def import_candidates(
        self, draft_id: str, request: ImportObjectCandidatesRequest
    ) -> OntologyDraftAggregate:
        """Import only explicitly selected metadata or VERIFIED field candidates."""

        aggregate = self._editable(draft_id)
        generated = self.generate_candidates(draft_id)
        available: dict[str, object] = {}
        for candidate in generated.object_types:
            available[candidate.candidate_id] = candidate.object_type
        for candidate in generated.properties:
            available[candidate.candidate_id] = candidate.property
        for candidate in generated.bindings:
            available[candidate.candidate_id] = candidate.binding
        for candidate in generated.physical_joins:
            available[candidate.candidate_id] = candidate.physical_join
        for candidate in generated.link_types:
            available[candidate.candidate_id] = candidate.link_type

        if self.candidate_repository is not None:
            verified = self.candidate_repository.list_candidates(
                status=ReviewStatus.VERIFIED,
                snapshot_id=aggregate.draft.source_snapshot_id,
                limit=10_000,
            )
            binding_objects = {
                binding.table_name: binding.object_type_id
                for binding in aggregate.resources.bindings
            }
            for envelope in verified:
                if envelope.candidate_type != "concept":
                    continue
                candidate = CandidateConcept.model_validate(envelope.payload)
                object_id = binding_objects.get(candidate.table_name)
                if object_id:
                    available[envelope.id] = self._property_from_verified_candidate(
                        object_id, candidate
                    )

        missing = set(request.candidate_ids) - available.keys()
        if missing:
            raise OntologyError(
                "Selected candidates are unavailable or not VERIFIED: "
                + ", ".join(sorted(missing))
            )
        for candidate_id in request.candidate_ids:
            resource = available[candidate_id]
            current = self.get_draft(draft_id).resources
            collection = {
                ObjectType: current.object_types,
                PropertyDefinition: current.properties,
                LinkType: current.link_types,
                ObjectDataSourceBinding: current.bindings,
                PhysicalJoinDefinition: current.physical_joins,
            }[type(resource)]
            previous = next((item for item in collection if item.id == resource.id), None)
            if previous is not None and not isinstance(resource, ObjectDataSourceBinding):
                continue
            self.save_resource(draft_id, resource)
        refreshed = self.get_draft(draft_id)
        refreshed.draft.validation_report = None
        refreshed.draft.updated_at = datetime.now(UTC)
        self.repository.save_draft(refreshed.draft)
        return self.get_draft(draft_id)

    @staticmethod
    def _property_from_verified_candidate(
        object_id: str, candidate: CandidateConcept
    ) -> PropertyDefinition:
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
        return PropertyDefinition(
            id=f"{object_id}.{candidate.column_name}",
            object_type_id=object_id,
            name=candidate.business_name,
            description=candidate.semantic_property,
            data_type=data_type,
            semantic_role=role,
            nullable=role != SemanticRole.IDENTIFIER,
            groupable=role in {SemanticRole.DIMENSION, SemanticRole.STATUS},
            unit=candidate.unit,
            synonyms=candidate.synonyms,
        )

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
