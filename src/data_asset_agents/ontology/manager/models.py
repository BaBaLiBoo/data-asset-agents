"""Strongly typed, object-first ontology manager contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from data_asset_agents.ontology.models import MetricAggregation, PropertyFilterPredicate

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
PHYSICAL_IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


def utc_now() -> datetime:
    return datetime.now(UTC)


class LifecycleStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class PropertyDataType(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    DATETIME = "DATETIME"


class SemanticRole(StrEnum):
    IDENTIFIER = "IDENTIFIER"
    ATTRIBUTE = "ATTRIBUTE"
    STATUS = "STATUS"
    MEASURE = "MEASURE"
    DIMENSION = "DIMENSION"
    TIME = "TIME"


class Cardinality(StrEnum):
    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    MANY_TO_MANY = "MANY_TO_MANY"


class DraftStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"


class ValidationState(StrEnum):
    NEVER_VALIDATED = "NEVER_VALIDATED"
    VALID = "VALID"
    STALE = "STALE"
    FAILED = "FAILED"


class CompiledArtifactStatus(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"


class OntologyAuditAction(StrEnum):
    DRAFT_CREATED = "DRAFT_CREATED"
    RESOURCE_CREATED = "RESOURCE_CREATED"
    RESOURCE_UPDATED = "RESOURCE_UPDATED"
    RESOURCE_DELETED = "RESOURCE_DELETED"
    CANDIDATES_IMPORTED = "CANDIDATES_IMPORTED"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PUBLISHED = "PUBLISHED"
    ACTIVATED = "ACTIVATED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class BindingSyncStatus(StrEnum):
    UNCHECKED = "UNCHECKED"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DRIFTED = "DRIFTED"
    FAILED = "FAILED"


class TableRole(StrEnum):
    CANONICAL_OBJECT = "CANONICAL_OBJECT"
    EVENT = "EVENT"
    AGGREGATE_VIEW = "AGGREGATE_VIEW"
    TECHNICAL = "TECHNICAL"
    DEPRECATED = "DEPRECATED"


ResourceId = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
PhysicalIdentifier = Annotated[str, Field(pattern=PHYSICAL_IDENTIFIER_PATTERN)]


class ObjectType(BaseModel):
    id: ResourceId
    name: str = Field(min_length=1, max_length=160)
    plural_name: str = Field(min_length=1, max_length=160)
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    primary_key_property_id: str | None = None
    title_property_id: str | None = None
    property_ids: list[str] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PropertyDefinition(BaseModel):
    id: ResourceId
    object_type_id: ResourceId
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    data_type: PropertyDataType
    semantic_role: SemanticRole
    nullable: bool = True
    filterable: bool = True
    groupable: bool = False
    sensitive: bool = False
    unit: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DimensionDefinition(BaseModel):
    """Draft-time analytical dimension with no editable physical coordinates."""

    id: ResourceId
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    property_id: ResourceId
    synonyms: list[str] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MetricDefinition(BaseModel):
    """Draft-time metric expressed only through governed semantic resources."""

    id: ResourceId
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    measure_property_id: ResourceId
    aggregation: MetricAggregation
    filter_predicates: list[PropertyFilterPredicate] = Field(default_factory=list)
    time_property_id: ResourceId | None = None
    supported_dimension_ids: list[ResourceId] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PhysicalJoinDefinition(BaseModel):
    id: ResourceId
    name: str = ""
    description: str = ""
    left_data_source_id: ResourceId = "minibank-postgres"
    left_schema: PhysicalIdentifier = "public"
    left_table: PhysicalIdentifier
    left_column: PhysicalIdentifier
    right_data_source_id: ResourceId = "minibank-postgres"
    right_schema: PhysicalIdentifier = "public"
    right_table: PhysicalIdentifier
    right_column: PhysicalIdentifier
    relationship: str = "many_to_one"
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    enabled: bool = True
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE

    @property
    def expression(self) -> str:
        return f"{self.left_table}.{self.left_column} = {self.right_table}.{self.right_column}"


class LinkType(BaseModel):
    id: ResourceId
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    source_object_type_id: ResourceId
    target_object_type_id: ResourceId
    source_role_name: str
    target_role_name: str
    cardinality: Cardinality
    physical_join_ids: list[str] = Field(min_length=1)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DataSourceDefinition(BaseModel):
    id: ResourceId
    name: str
    provider: Literal["POSTGRESQL"] = "POSTGRESQL"
    connection_ref: str = "DATABASE_URL"
    enabled: bool = True
    description: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("connection_ref")
    @classmethod
    def connection_ref_is_environment_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", value):
            raise ValueError("connection_ref must be an environment variable name")
        if "://" in value or "=" in value:
            raise ValueError("connection_ref cannot contain a connection string")
        return value


class ObjectDataSourceBinding(BaseModel):
    id: ResourceId
    object_type_id: ResourceId
    data_source_id: ResourceId
    schema_name: PhysicalIdentifier = "public"
    table_name: PhysicalIdentifier
    primary_key_column: PhysicalIdentifier
    property_bindings: dict[str, str] = Field(default_factory=dict)
    latest_snapshot_id: str | None = None
    schema_hash: str
    sync_status: BindingSyncStatus = BindingSyncStatus.UNCHECKED
    last_inspected_at: datetime | None = None
    error_message: str | None = None
    schema_columns: dict[str, str] = Field(default_factory=dict)
    primary_key_columns: list[str] = Field(default_factory=list)

    @field_validator("property_bindings")
    @classmethod
    def validate_physical_columns(cls, value: dict[str, str]) -> dict[str, str]:
        for column in value.values():
            if not re.fullmatch(PHYSICAL_IDENTIFIER_PATTERN, column):
                raise ValueError(f"unsafe physical column identifier: {column}")
        return value


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["ERROR", "WARNING"] = "ERROR"
    resource_type: str
    resource_id: str
    field: str | None = None
    message: str
    suggested_fix: str


class DraftValidationReport(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)
    benchmark_sql: str | None = None
    explain_passed: bool | None = None
    dry_run_cases: list[dict[str, object]] = Field(default_factory=list)
    resource_revision: int = 0
    resource_hash: str = ""
    compiler_version: str = "1"
    validation_run_id: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)


class OntologyDraft(BaseModel):
    id: ResourceId
    name: str
    description: str = ""
    base_version_id: str | None = None
    source_snapshot_id: str | None = None
    status: DraftStatus = DraftStatus.DRAFT
    created_by: str
    submitted_by: str | None = None
    reviewed_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    validation_report: DraftValidationReport | None = None
    rejection_reason: str | None = None
    resource_revision: int = Field(default=0, ge=0)
    resource_hash: str = ""
    validated_revision: int | None = None
    validated_hash: str | None = None
    submitted_revision: int | None = None
    submitted_hash: str | None = None
    validation_state: ValidationState = ValidationState.NEVER_VALIDATED


class DraftResources(BaseModel):
    object_types: list[ObjectType] = Field(default_factory=list)
    properties: list[PropertyDefinition] = Field(default_factory=list)
    link_types: list[LinkType] = Field(default_factory=list)
    bindings: list[ObjectDataSourceBinding] = Field(default_factory=list)
    physical_joins: list[PhysicalJoinDefinition] = Field(default_factory=list)
    metrics: list[MetricDefinition] = Field(default_factory=list)
    dimensions: list[DimensionDefinition] = Field(default_factory=list)


class OntologyDraftAggregate(BaseModel):
    draft: OntologyDraft
    resources: DraftResources = Field(default_factory=DraftResources)


class CompiledOntologyArtifact(BaseModel):
    artifact_id: str
    ontology_version_id: str
    source_draft_id: str
    source_revision: int
    source_resource_hash: str
    compiler_name: str
    compiler_version: str
    compiler_source_hash: str
    status: CompiledArtifactStatus
    bundle_hash: str
    bundle_json: dict[str, object]
    property_bindings: dict[str, str] = Field(default_factory=dict)
    metric_compilation_evidence: list[dict[str, object]] = Field(default_factory=list)
    dimension_compilation_evidence: list[dict[str, object]] = Field(default_factory=list)
    join_compilation_evidence: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    error_message: str | None = None


class CompiledArtifactSummary(BaseModel):
    artifact_id: str
    ontology_version_id: str
    source_revision: int
    source_resource_hash: str
    compiler_version: str
    bundle_hash: str
    status: CompiledArtifactStatus
    created_at: datetime
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    compilation_evidence: dict[str, object] = Field(default_factory=dict)
    bundle_json: dict[str, object] | None = None


class OntologyAuditEvent(BaseModel):
    event_id: str
    draft_id: str | None = None
    ontology_version_id: str | None = None
    actor: str
    action: OntologyAuditAction
    resource_type: str | None = None
    resource_id: str | None = None
    before_revision: int | None = None
    after_revision: int | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, object] = Field(default_factory=dict)


class CreateDraftRequest(BaseModel):
    name: str
    description: str = ""
    base_version_id: str | None = None
    source_snapshot_id: str | None = None
    created_by: str = "ontology-manager"


class CreateDraftFromSeedRequest(BaseModel):
    draft_name: str = "MiniBank object-first model"
    created_by: str = "ontology-manager"
    source_snapshot_id: str | None = None
    seed_name: str = Field(default="retail_banking", pattern=r"^[a-z][a-z0-9_-]{1,63}$")


class ActorRequest(BaseModel):
    actor: str = "ontology-reviewer"


class RejectDraftRequest(ActorRequest):
    reason: str = Field(min_length=1)


class PublishDraftRequest(ActorRequest):
    version: str = Field(max_length=50, pattern=r"^[A-Za-z0-9_.-]{1,50}$")
    description: str = "Object-first ontology manager publication"
    acknowledge_breaking_changes: bool = False
    change_ticket: str | None = None


class MigrateLegacyRequest(BaseModel):
    draft_name: str = "MiniBank legacy object migration"
    created_by: str = "legacy-migrator"
    source_snapshot_id: str | None = None
    dry_run: bool = False


class CandidateEvidence(BaseModel):
    source: str
    detail: str


class CandidateObjectType(BaseModel):
    candidate_id: str
    object_type: ObjectType
    table_role: TableRole
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class CandidateProperty(BaseModel):
    candidate_id: str
    property: PropertyDefinition
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class CandidateLinkType(BaseModel):
    candidate_id: str
    link_type: LinkType
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class CandidateObjectBinding(BaseModel):
    candidate_id: str
    binding: ObjectDataSourceBinding
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class CandidatePhysicalJoin(BaseModel):
    candidate_id: str
    physical_join: PhysicalJoinDefinition
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)


class ObjectCandidateSet(BaseModel):
    snapshot_id: str
    object_types: list[CandidateObjectType] = Field(default_factory=list)
    properties: list[CandidateProperty] = Field(default_factory=list)
    bindings: list[CandidateObjectBinding] = Field(default_factory=list)
    link_types: list[CandidateLinkType] = Field(default_factory=list)
    physical_joins: list[CandidatePhysicalJoin] = Field(default_factory=list)
    excluded_tables: dict[str, str] = Field(default_factory=dict)


class ImportObjectCandidatesRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    actor: str = Field(default="ontology-manager", min_length=1, max_length=100)


class ObjectCandidateLLMOutput(BaseModel):
    """Provider-neutral Structured Output; never a publishable resource by itself."""

    object_name: str
    boundary_description: str
    property_names: list[str]
    property_roles: dict[str, SemanticRole]
    link_business_names: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class DataSourceInspection(BaseModel):
    data_source_id: str
    healthy: bool
    provider: str
    schemas: list[str]
    table_count: int
    inspected_at: datetime = Field(default_factory=utc_now)
    error: str | None = None


class ObjectGraph(BaseModel):
    version_id: str | None
    nodes: list[dict[str, str]]
    edges: list[dict[str, str]]


class DraftResourceMutation(BaseModel):
    resource_type: Literal[
        "object_type",
        "property",
        "link_type",
        "binding",
        "physical_join",
        "metric",
        "dimension",
    ]
    resource_id: str

    @model_validator(mode="after")
    def safe_id(self) -> DraftResourceMutation:
        if not re.fullmatch(IDENTIFIER_PATTERN, self.resource_id):
            raise ValueError("invalid resource id")
        return self
