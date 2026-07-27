"""Application service for isolated editing, review and atomic publication."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Engine, inspect

from data_asset_agents.core.errors import (
    OntologyConflictError,
    OntologyError,
    OntologyGovernanceError,
)
from data_asset_agents.ontology.models import (
    CandidateConcept,
    OntologyBundle,
    OntologyVersion,
    ReviewStatus,
)
from data_asset_agents.ontology.validation import OntologyContractValidator

from .candidate_generator import ObjectFirstCandidateGenerator
from .change_analysis import OntologyChangeAnalyzer
from .compiler import (
    COMPILER_NAME,
    COMPILER_VERSION,
    ObjectSemanticCompiler,
    compiler_source_hash,
)
from .governance_models import OntologyChangeSet, OntologyImpactReport
from .hashing import calculate_bundle_hash, calculate_draft_resource_hash
from .migration import LegacyOntologyObjectMigrator
from .models import (
    ActorRequest,
    BindingSyncStatus,
    CompiledArtifactStatus,
    CompiledArtifactSummary,
    CompiledOntologyArtifact,
    ConstructionMode,
    CreateDraftFromSeedRequest,
    CreateDraftRequest,
    DataSourceDefinition,
    DataSourceInspection,
    DimensionDefinition,
    DraftResources,
    DraftStatus,
    DraftValidationReport,
    ImportObjectCandidatesRequest,
    LinkType,
    MetricDefinition,
    MigrateLegacyRequest,
    ObjectCandidateSet,
    ObjectDataSourceBinding,
    ObjectGraph,
    ObjectType,
    OntologyAuditAction,
    OntologyAuditEvent,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    RejectDraftRequest,
    SemanticRole,
    ValidationIssue,
    ValidationState,
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

    def delete_draft(
        self, draft_id: str, *, expected_revision: int | None = None
    ) -> None:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status == DraftStatus.PUBLISHED:
            raise OntologyConflictError("Published Drafts are immutable and cannot be deleted")
        self.repository.delete_draft(draft_id, expected_revision)

    def _editable(self, draft_id: str) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.status != DraftStatus.DRAFT:
            raise OntologyConflictError(
                f"Draft {draft_id} is {aggregate.draft.status}; only DRAFT resources are editable"
            )
        return aggregate

    @staticmethod
    def _resource_collection(resources: DraftResources, resource: object) -> list[object]:
        return {
            ObjectType: resources.object_types,
            PropertyDefinition: resources.properties,
            LinkType: resources.link_types,
            ObjectDataSourceBinding: resources.bindings,
            PhysicalJoinDefinition: resources.physical_joins,
            MetricDefinition: resources.metrics,
            DimensionDefinition: resources.dimensions,
        }[type(resource)]

    def _save_resource_to_snapshot(
        self, resources: DraftResources, resource: object
    ) -> None:
        if isinstance(resource, PropertyDefinition):
            obj = next(
                (item for item in resources.object_types if item.id == resource.object_type_id),
                None,
            )
            if obj is None:
                raise OntologyError(f"Property object does not exist: {resource.object_type_id}")
            if resource.id not in obj.property_ids:
                resources.object_types[:] = [
                    item
                    if item.id != obj.id
                    else obj.model_copy(
                        update={
                            "property_ids": [*obj.property_ids, resource.id],
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    for item in resources.object_types
                ]
        if isinstance(resource, DimensionDefinition) and not any(
            item.id == resource.property_id for item in resources.properties
        ):
            raise OntologyError(
                f"Dimension Property does not exist: {resource.property_id}"
            )
        if isinstance(resource, MetricDefinition):
            property_ids = {item.id for item in resources.properties}
            referenced_properties = {
                resource.measure_property_id,
                resource.time_property_id,
                *(item.property_id for item in resource.filter_predicates),
            } - {None}
            missing_properties = referenced_properties - property_ids
            if missing_properties:
                raise OntologyError(
                    f"Metric references missing Properties: {sorted(missing_properties)}"
                )
            dimension_ids = {item.id for item in resources.dimensions}
            missing_dimensions = set(resource.supported_dimension_ids) - dimension_ids
            if missing_dimensions:
                raise OntologyError(
                    f"Metric references missing Dimensions: {sorted(missing_dimensions)}"
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
        collection = self._resource_collection(resources, resource)
        collection[:] = [item for item in collection if item.id != resource.id]
        collection.append(resource)

    def save_resource(
        self,
        draft_id: str,
        resource: object,
        *,
        expected_revision: int | None = None,
        actor: str = "ontology-manager",
        request_id: str | None = None,
    ) -> OntologyDraftAggregate:
        aggregate = self._editable(draft_id)
        collection = self._resource_collection(aggregate.resources, resource)
        action = (
            OntologyAuditAction.RESOURCE_UPDATED
            if any(item.id == resource.id for item in collection)
            else OntologyAuditAction.RESOURCE_CREATED
        )
        return self.repository.apply_mutation(
            draft_id,
            expected_revision,
            actor,
            action,
            type(resource).__name__,
            resource.id,
            lambda resources: self._save_resource_to_snapshot(resources, resource),
            request_id=request_id,
        )

    def delete_resource(
        self,
        draft_id: str,
        kind: str,
        resource_id: str,
        *,
        expected_revision: int | None = None,
        actor: str = "ontology-manager",
        request_id: str | None = None,
    ) -> OntologyDraftAggregate:
        editable = self._editable(draft_id)

        def delete_from_snapshot(resources: DraftResources) -> None:
            aggregate = OntologyDraftAggregate(
                draft=editable.draft, resources=resources
            )
            self._delete_resource_from_snapshot(aggregate, kind, resource_id)

        return self.repository.apply_mutation(
            draft_id,
            expected_revision,
            actor,
            OntologyAuditAction.RESOURCE_DELETED,
            kind,
            resource_id,
            delete_from_snapshot,
            request_id=request_id,
        )

    def _delete_resource_from_snapshot(
        self, aggregate: OntologyDraftAggregate, kind: str, resource_id: str
    ) -> None:
        if kind == "object_type":
            property_ids = {
                prop.id
                for prop in aggregate.resources.properties
                if prop.object_type_id == resource_id
            }
            deleted_dimensions = {
                dimension.id
                for dimension in aggregate.resources.dimensions
                if dimension.property_id in property_ids
            }
            aggregate.resources.metrics[:] = [
                metric
                for metric in aggregate.resources.metrics
                if not (
                    {
                        metric.measure_property_id,
                        metric.time_property_id,
                        *(item.property_id for item in metric.filter_predicates),
                    }
                    & property_ids
                    or set(metric.supported_dimension_ids) & deleted_dimensions
                )
            ]
            aggregate.resources.dimensions[:] = [
                item for item in aggregate.resources.dimensions if item.id not in deleted_dimensions
            ]
            aggregate.resources.properties[:] = [
                item
                for item in aggregate.resources.properties
                if item.object_type_id != resource_id
            ]
            aggregate.resources.bindings[:] = [
                item
                for item in aggregate.resources.bindings
                if item.object_type_id != resource_id
            ]
            aggregate.resources.link_types[:] = [
                item
                for item in aggregate.resources.link_types
                if resource_id
                not in {item.source_object_type_id, item.target_object_type_id}
            ]
        if kind == "property":
            referencing_metrics = [
                metric.id
                for metric in aggregate.resources.metrics
                if resource_id
                in {
                    metric.measure_property_id,
                    metric.time_property_id,
                    *(item.property_id for item in metric.filter_predicates),
                }
            ]
            referencing_dimensions = [
                dimension.id
                for dimension in aggregate.resources.dimensions
                if dimension.property_id == resource_id
            ]
            if referencing_metrics or referencing_dimensions:
                raise OntologyConflictError(
                    "Property is referenced by analysis semantics; reassign or delete first: "
                    + ", ".join(sorted([*referencing_metrics, *referencing_dimensions]))
                )
            for obj in aggregate.resources.object_types:
                if resource_id in obj.property_ids:
                    if resource_id in {obj.primary_key_property_id, obj.title_property_id}:
                        raise OntologyConflictError(
                            "Primary/title properties must be reassigned before deletion"
                        )
                    aggregate.resources.object_types[:] = [
                        item
                        if item.id != obj.id
                        else obj.model_copy(
                            update={
                                "property_ids": [
                                    x for x in obj.property_ids if x != resource_id
                                ]
                            }
                        )
                        for item in aggregate.resources.object_types
                    ]
        if kind == "dimension":
            referencing_metrics = [
                metric.id
                for metric in aggregate.resources.metrics
                if resource_id in metric.supported_dimension_ids
            ]
            if referencing_metrics:
                raise OntologyConflictError(
                    "Dimension is referenced by Metric(s): "
                    + ", ".join(sorted(referencing_metrics))
                )
        collection = {
            "object_type": aggregate.resources.object_types,
            "property": aggregate.resources.properties,
            "link_type": aggregate.resources.link_types,
            "binding": aggregate.resources.bindings,
            "physical_join": aggregate.resources.physical_joins,
            "metric": aggregate.resources.metrics,
            "dimension": aggregate.resources.dimensions,
        }[kind]
        collection[:] = [item for item in collection if item.id != resource_id]

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
        self,
        draft_id: str,
        request: ImportObjectCandidatesRequest,
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
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
        for candidate in generated.dimensions:
            available[candidate.candidate_id] = candidate.dimension
        for candidate in generated.metrics:
            available[candidate.candidate_id] = candidate.metric

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
        selected = [
            (candidate_id, available[candidate_id])
            for candidate_id in request.candidate_ids
        ]

        def import_all(resources: DraftResources) -> None:
            for _, resource in selected:
                collection = self._resource_collection(resources, resource)
                previous = next((item for item in collection if item.id == resource.id), None)
                if previous is not None and not isinstance(resource, ObjectDataSourceBinding):
                    continue
                self._save_resource_to_snapshot(resources, resource)

        return self.repository.apply_mutation(
            draft_id,
            expected_revision,
            request.actor,
            OntologyAuditAction.CANDIDATES_IMPORTED,
            "candidate_batch",
            None,
            import_all,
            request_id=request_id,
            metadata={"candidate_ids": request.candidate_ids},
        )

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

    @staticmethod
    def _check_expected_revision(
        aggregate: OntologyDraftAggregate, expected_revision: int | None
    ) -> None:
        if (
            expected_revision is not None
            and aggregate.draft.resource_revision != expected_revision
        ):
            raise OntologyConflictError(
                "Draft 已被其他操作更新，请刷新后重新编辑。",
                "DRAFT_REVISION_CONFLICT",
                current_revision=aggregate.draft.resource_revision,
                current_hash=aggregate.draft.resource_hash,
            )

    def validate(
        self,
        draft_id: str,
        *,
        expected_revision: int | None = None,
        actor: str = "ontology-validator",
        request_id: str | None = None,
        construction_mode: ConstructionMode = ConstructionMode.LEGACY_COMPAT,
    ) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        if aggregate.draft.construction_run_id:
            construction_mode = ConstructionMode.STRICT_CONSTRUCTION
        self._check_expected_revision(aggregate, expected_revision)
        if aggregate.draft.status in {DraftStatus.PUBLISHED, DraftStatus.REJECTED}:
            raise OntologyConflictError(f"Cannot validate a {aggregate.draft.status} Draft")
        starting_revision = aggregate.draft.resource_revision
        starting_hash = aggregate.draft.resource_hash
        if calculate_draft_resource_hash(aggregate.resources) != starting_hash:
            current = self.get_draft(draft_id).draft
            raise OntologyGovernanceError(
                "Draft changed while its validation snapshot was being read",
                "DRAFT_CHANGED_DURING_VALIDATION",
                current_revision=current.resource_revision,
                current_hash=current.resource_hash,
            )
        started_at = datetime.now(UTC)
        validation_run_id = f"validation-{uuid4().hex}"
        self.repository.append_audit_event(
            OntologyAuditEvent(
                event_id=f"audit-{uuid4().hex}",
                draft_id=draft_id,
                actor=actor,
                action=OntologyAuditAction.VALIDATION_STARTED,
                before_revision=starting_revision,
                after_revision=starting_revision,
                before_hash=starting_hash,
                after_hash=starting_hash,
                request_id=request_id,
                metadata={"validation_run_id": validation_run_id},
            )
        )
        try:
            if construction_mode == ConstructionMode.STRICT_CONSTRUCTION:
                report = self.validator.validate(
                    aggregate.resources,
                    aggregate.draft.source_snapshot_id,
                    construction_mode=construction_mode,
                )
            else:
                report = self.validator.validate(
                    aggregate.resources, aggregate.draft.source_snapshot_id
                )
        except Exception as exc:
            report = DraftValidationReport(
                valid=False,
                issues=[
                    ValidationIssue(
                        code="VALIDATION_EXECUTION_FAILED",
                        resource_type="draft",
                        resource_id=draft_id,
                        message=f"Validation execution failed ({type(exc).__name__})",
                        suggested_fix="Restore validator/database health and run validation again",
                    )
                ],
            )
        report = report.model_copy(
            update={
                "resource_revision": starting_revision,
                "resource_hash": starting_hash,
                "compiler_version": COMPILER_VERSION,
                "validation_run_id": validation_run_id,
                "started_at": started_at,
                "completed_at": datetime.now(UTC),
            }
        )
        aggregate.draft.validation_report = report
        aggregate.draft.validated_revision = starting_revision
        aggregate.draft.validated_hash = starting_hash
        aggregate.draft.validation_state = (
            ValidationState.VALID if report.valid else ValidationState.FAILED
        )
        aggregate.draft.updated_at = datetime.now(UTC)
        return self.repository.update_draft_state(
            aggregate.draft,
            starting_revision,
            starting_hash,
            actor,
            (
                OntologyAuditAction.VALIDATION_PASSED
                if report.valid
                else OntologyAuditAction.VALIDATION_FAILED
            ),
            request_id=request_id,
            metadata={"validation_run_id": validation_run_id},
        )

    def submit(
        self,
        draft_id: str,
        request: ActorRequest,
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> OntologyDraftAggregate:
        aggregate = self._editable(draft_id)
        self._check_expected_revision(aggregate, expected_revision)
        draft = aggregate.draft
        if draft.validation_state == ValidationState.STALE:
            raise OntologyGovernanceError(
                "Validation report is stale for the current Draft",
                "DRAFT_VALIDATION_STALE",
                current_revision=draft.resource_revision,
                current_hash=draft.resource_hash,
            )
        if draft.validation_report is None:
            raise OntologyGovernanceError(
                "Draft must be validated before submission",
                "DRAFT_NOT_VALIDATED",
                current_revision=draft.resource_revision,
                current_hash=draft.resource_hash,
            )
        if not draft.validation_report.valid or draft.validation_state == ValidationState.FAILED:
            raise OntologyGovernanceError(
                "Failed validation cannot be submitted",
                "DRAFT_VALIDATION_FAILED",
                current_revision=draft.resource_revision,
                current_hash=draft.resource_hash,
            )
        if (
            draft.validation_state != ValidationState.VALID
            or draft.validated_revision != draft.resource_revision
            or draft.validated_hash != draft.resource_hash
        ):
            raise OntologyGovernanceError(
                "Validation report is stale for the current Draft",
                "DRAFT_VALIDATION_STALE",
                current_revision=draft.resource_revision,
                current_hash=draft.resource_hash,
            )
        aggregate.draft.status = DraftStatus.IN_REVIEW
        aggregate.draft.submitted_by = request.actor
        aggregate.draft.submitted_at = datetime.now(UTC)
        aggregate.draft.submitted_revision = aggregate.draft.resource_revision
        aggregate.draft.submitted_hash = aggregate.draft.resource_hash
        aggregate.draft.updated_at = aggregate.draft.submitted_at
        return self.repository.update_draft_state(
            aggregate.draft,
            aggregate.draft.resource_revision,
            aggregate.draft.resource_hash,
            request.actor,
            OntologyAuditAction.SUBMITTED,
            request_id=request_id,
        )

    def approve(
        self,
        draft_id: str,
        request: ActorRequest,
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        self._check_expected_revision(aggregate, expected_revision)
        if aggregate.draft.status != DraftStatus.IN_REVIEW:
            raise OntologyConflictError("Only IN_REVIEW Drafts can be approved")
        if (
            calculate_draft_resource_hash(aggregate.resources) != aggregate.draft.resource_hash
            or aggregate.draft.resource_revision != aggregate.draft.submitted_revision
            or aggregate.draft.resource_hash != aggregate.draft.submitted_hash
        ):
            raise OntologyGovernanceError(
                "Review snapshot no longer matches submitted content",
                "REVIEW_SNAPSHOT_CHANGED",
                current_revision=aggregate.draft.resource_revision,
                current_hash=aggregate.draft.resource_hash,
            )
        review_validation_started = datetime.now(UTC)
        try:
            report = self.validator.validate(
                aggregate.resources, aggregate.draft.source_snapshot_id
            )
        except Exception as exc:
            report = DraftValidationReport(
                valid=False,
                issues=[
                    ValidationIssue(
                        code="VALIDATION_EXECUTION_FAILED",
                        resource_type="draft",
                        resource_id=draft_id,
                        message=f"Review validation failed ({type(exc).__name__})",
                        suggested_fix="Restore validator/database health and review a new Draft",
                    )
                ],
            )
        report = report.model_copy(
            update={
                "resource_revision": aggregate.draft.resource_revision,
                "resource_hash": aggregate.draft.resource_hash,
                "compiler_version": COMPILER_VERSION,
                "validation_run_id": f"review-validation-{uuid4().hex}",
                "started_at": review_validation_started,
                "completed_at": datetime.now(UTC),
            }
        )
        if not report.valid:
            aggregate.draft.validation_report = report
            aggregate.draft.validation_state = ValidationState.FAILED
            self.repository.update_draft_state(
                aggregate.draft,
                aggregate.draft.resource_revision,
                aggregate.draft.resource_hash,
                request.actor,
                OntologyAuditAction.VALIDATION_FAILED,
                request_id=request_id,
            )
            raise OntologyError("Draft approval failed validation")
        aggregate.draft.status = DraftStatus.VALIDATED
        aggregate.draft.validation_report = report
        aggregate.draft.validation_state = ValidationState.VALID
        aggregate.draft.validated_revision = aggregate.draft.resource_revision
        aggregate.draft.validated_hash = aggregate.draft.resource_hash
        aggregate.draft.reviewed_by = request.actor
        aggregate.draft.reviewed_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.reviewed_at
        return self.repository.update_draft_state(
            aggregate.draft,
            aggregate.draft.resource_revision,
            aggregate.draft.resource_hash,
            request.actor,
            OntologyAuditAction.APPROVED,
            request_id=request_id,
        )

    def reject(
        self,
        draft_id: str,
        request: RejectDraftRequest,
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> OntologyDraftAggregate:
        aggregate = self.get_draft(draft_id)
        self._check_expected_revision(aggregate, expected_revision)
        if aggregate.draft.status != DraftStatus.IN_REVIEW:
            raise OntologyConflictError("Only IN_REVIEW Drafts can be rejected")
        aggregate.draft.status = DraftStatus.REJECTED
        aggregate.draft.reviewed_by = request.actor
        aggregate.draft.rejection_reason = request.reason
        aggregate.draft.reviewed_at = datetime.now(UTC)
        aggregate.draft.updated_at = aggregate.draft.reviewed_at
        return self.repository.update_draft_state(
            aggregate.draft,
            aggregate.draft.resource_revision,
            aggregate.draft.resource_hash,
            request.actor,
            OntologyAuditAction.REJECTED,
            request_id=request_id,
            metadata={"reason_present": True},
        )

    def publish(
        self,
        draft_id: str,
        request: PublishDraftRequest,
        *,
        expected_revision: int | None = None,
    ) -> OntologyVersion:
        aggregate = self.get_draft(draft_id)
        self._check_expected_revision(aggregate, expected_revision)
        if aggregate.draft.status != DraftStatus.VALIDATED:
            raise OntologyConflictError("Only a VALIDATED Draft can be published")
        if (
            calculate_draft_resource_hash(aggregate.resources) != aggregate.draft.resource_hash
            or aggregate.draft.resource_revision != aggregate.draft.submitted_revision
            or aggregate.draft.resource_hash != aggregate.draft.submitted_hash
        ):
            raise OntologyGovernanceError(
                "Publish content differs from the reviewed snapshot",
                "REVIEW_SNAPSHOT_CHANGED",
                current_revision=aggregate.draft.resource_revision,
                current_hash=aggregate.draft.resource_hash,
            )
        report = aggregate.draft.validation_report
        if (
            report is None
            or not report.valid
            or report.resource_hash != aggregate.draft.resource_hash
            or report.resource_revision != aggregate.draft.resource_revision
            or aggregate.draft.validation_state != ValidationState.VALID
        ):
            raise OntologyGovernanceError(
                "Publish requires validation of the exact reviewed snapshot",
                "DRAFT_VALIDATION_STALE",
                current_revision=aggregate.draft.resource_revision,
                current_hash=aggregate.draft.resource_hash,
            )
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
        mode = report.construction_mode
        compilation = ObjectSemanticCompiler(
            None if mode == ConstructionMode.STRICT_CONSTRUCTION else self.base_bundle,
            construction_mode=mode,
        ).compile(aggregate.resources)
        if mode == ConstructionMode.STRICT_CONSTRUCTION and (
            compilation.seed_accessed
            or compilation.fallback_used
            or compilation.legacy_ontology_accessed
        ):
            raise OntologyError("Strict construction leakage flags invalidate publication")
        if compilation.conflicts:
            raise OntologyError("; ".join(compilation.conflicts))
        bundle = compilation.bundle
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
        bundle_hash = calculate_bundle_hash(bundle)
        artifact = CompiledOntologyArtifact(
            artifact_id=f"artifact-{version.id}",
            ontology_version_id=version.id,
            source_draft_id=aggregate.draft.id,
            source_revision=aggregate.draft.resource_revision,
            source_resource_hash=aggregate.draft.resource_hash,
            construction_run_id=aggregate.draft.construction_run_id,
            compiler_name=COMPILER_NAME,
            compiler_version=COMPILER_VERSION,
            compiler_source_hash=compiler_source_hash(),
            status=CompiledArtifactStatus.READY,
            bundle_hash=bundle_hash,
            bundle_json=bundle.model_dump(mode="json"),
            property_bindings=compilation.property_bindings,
            metric_compilation_evidence=compilation.metric_evidence,
            dimension_compilation_evidence=compilation.dimension_evidence,
            join_compilation_evidence=compilation.join_evidence,
            construction_evidence_summary={
                "metrics": len(compilation.metric_evidence),
                "dimensions": len(compilation.dimension_evidence),
                "joins": len(compilation.join_evidence),
            },
        )
        self.repository.publish(
            aggregate.draft, aggregate.resources, version, bundle, artifact
        )
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

    def compiled_artifact(
        self, version_id: str, *, include_bundle: bool = False
    ) -> CompiledArtifactSummary:
        artifact = self.repository.get_compiled_artifact(version_id)
        if artifact is None:
            raise OntologyError(f"Compiled ontology artifact not found: {version_id}")
        return CompiledArtifactSummary(
            artifact_id=artifact.artifact_id,
            ontology_version_id=artifact.ontology_version_id,
            source_revision=artifact.source_revision,
            source_resource_hash=artifact.source_resource_hash,
            compiler_version=artifact.compiler_version,
            bundle_hash=artifact.bundle_hash,
            status=artifact.status,
            created_at=artifact.created_at,
            evidence_summary={
                "property_bindings": len(artifact.property_bindings),
                "metrics": len(artifact.metric_compilation_evidence),
                "dimensions": len(artifact.dimension_compilation_evidence),
                "joins": len(artifact.join_compilation_evidence),
            },
            compilation_evidence={
                "property_bindings": artifact.property_bindings,
                "metrics": artifact.metric_compilation_evidence,
                "dimensions": artifact.dimension_compilation_evidence,
                "links": artifact.join_compilation_evidence,
            },
            bundle_json=artifact.bundle_json if include_bundle else None,
        )

    def load_runtime_bundle(
        self, version_id: str, legacy_bundle: OntologyBundle
    ) -> tuple[OntologyBundle, bool, CompiledOntologyArtifact | None]:
        """Load immutable output; compile only versions predating artifacts."""

        artifact = self.repository.get_compiled_artifact(version_id)
        if artifact is not None:
            if artifact.status != CompiledArtifactStatus.READY:
                raise OntologyGovernanceError(
                    "Compiled artifact is not READY", "ONTOLOGY_ARTIFACT_NOT_READY"
                )
            if artifact.ontology_version_id != version_id:
                raise OntologyGovernanceError(
                    "Compiled artifact version mismatch", "ONTOLOGY_ARTIFACT_VERSION_MISMATCH"
                )
            _, resources = self.repository.published_resources(version_id)
            published_hash = calculate_draft_resource_hash(resources)
            if published_hash != artifact.source_resource_hash:
                raise OntologyGovernanceError(
                    "Compiled artifact resource hash mismatch",
                    "ONTOLOGY_ARTIFACT_RESOURCE_HASH_MISMATCH",
                )
            try:
                bundle = OntologyBundle.model_validate(artifact.bundle_json)
            except ValidationError as exc:
                raise OntologyGovernanceError(
                    "Compiled artifact bundle is invalid",
                    "ONTOLOGY_ARTIFACT_BUNDLE_INVALID",
                ) from exc
            if calculate_bundle_hash(bundle) != artifact.bundle_hash:
                raise OntologyGovernanceError(
                    "Compiled artifact bundle hash mismatch",
                    "ONTOLOGY_ARTIFACT_BUNDLE_HASH_MISMATCH",
                )
            return bundle, False, artifact
        _, resources = self.repository.published_resources(version_id)
        if resources.object_types:
            compilation = ObjectSemanticCompiler(legacy_bundle).compile(resources)
            if compilation.conflicts:
                raise OntologyError("; ".join(compilation.conflicts))
            legacy_bundle = compilation.bundle
        contract = OntologyContractValidator(self.engine).validate(
            legacy_bundle,
            snapshot_id="legacy-runtime-fallback",
            source_candidates=[],
        )
        if not contract.valid:
            raise OntologyGovernanceError(
                "Legacy ontology fallback failed runtime contract validation",
                "LEGACY_ONTOLOGY_VALIDATION_FAILED",
            )
        return legacy_bundle, True, None

    def audit_events(
        self,
        *,
        draft_id: str | None = None,
        version_id: str | None = None,
        limit: int = 100,
    ) -> list[OntologyAuditEvent]:
        return self.repository.list_audit_events(
            draft_id=draft_id, version_id=version_id, limit=min(limit, 500)
        )

    def record_runtime_event(
        self,
        action: OntologyAuditAction,
        version_id: str,
        actor: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.repository.append_audit_event(
            OntologyAuditEvent(
                event_id=f"audit-{uuid4().hex}",
                ontology_version_id=version_id,
                actor=actor,
                action=action,
                metadata=metadata or {},
            )
        )

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
