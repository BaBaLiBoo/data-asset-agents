"""Strongly typed, object-first ontology manager contracts."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

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


class ConstructionMode(StrEnum):
    LEGACY_COMPAT = "LEGACY_COMPAT"
    STRICT_CONSTRUCTION = "STRICT_CONSTRUCTION"


class CatalogMode(StrEnum):
    RAW_METADATA = "RAW_METADATA"
    GOVERNED_CATALOG = "GOVERNED_CATALOG"


class ConstructionRunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    CANDIDATES_READY = "CANDIDATES_READY"
    UNDER_REVIEW = "UNDER_REVIEW"
    PROMOTED_TO_DRAFT = "PROMOTED_TO_DRAFT"
    EVALUATED = "EVALUATED"
    FAILED = "FAILED"


class ConstructionEvidenceMode(StrEnum):
    O_A_SCHEMA_ONLY = "O-A"
    O_B_SCHEMA_PROFILING = "O-B"
    O_C_SCHEMA_PROFILING_SQL = "O-C"
    O_D_ALL_EVIDENCE_LLM = "O-D"


class CandidateReviewDecision(StrEnum):
    ACCEPT = "ACCEPT"
    MODIFY = "MODIFY"
    REJECT = "REJECT"
    MERGE = "MERGE"
    DEFER = "DEFER"


class ConstructionCandidateStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    MODIFIED = "MODIFIED"
    REJECTED = "REJECTED"
    MERGED = "MERGED"
    DEFERRED = "DEFERRED"


class EvidenceSourceType(StrEnum):
    TABLE_METADATA = "TABLE_METADATA"
    COLUMN_METADATA = "COLUMN_METADATA"
    PRIMARY_KEY = "PRIMARY_KEY"
    FOREIGN_KEY = "FOREIGN_KEY"
    INDEX = "INDEX"
    FIELD_PROFILE = "FIELD_PROFILE"
    COLUMN_COMMENT = "COLUMN_COMMENT"
    CERTIFIED_HISTORICAL_SQL = "CERTIFIED_HISTORICAL_SQL"
    SQL_GROUP_BY = "SQL_GROUP_BY"
    SQL_AGGREGATION = "SQL_AGGREGATION"
    SQL_FILTER = "SQL_FILTER"
    SQL_JOIN = "SQL_JOIN"
    SQL_TIME_USAGE = "SQL_TIME_USAGE"
    GOVERNED_CATALOG = "GOVERNED_CATALOG"
    LLM_SEMANTIC_SUGGESTION = "LLM_SEMANTIC_SUGGESTION"
    HUMAN_REVIEW = "HUMAN_REVIEW"


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
    construction_mode: ConstructionMode = ConstructionMode.LEGACY_COMPAT
    seed_accessed: bool = False
    fallback_used: bool = False
    legacy_ontology_accessed: bool = False


class OntologyDraft(BaseModel):
    id: ResourceId
    name: str
    description: str = ""
    base_version_id: str | None = None
    source_snapshot_id: str | None = None
    construction_run_id: str | None = None
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
    construction_run_id: str | None = None
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
    construction_evidence_summary: dict[str, int] = Field(default_factory=dict)
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
    """Immutable evidence reference shared by every construction candidate.

    ``source`` and ``detail`` remain as read-compatible aliases for V2 clients.
    """

    evidence_id: str = ""
    source_type: EvidenceSourceType | str = EvidenceSourceType.TABLE_METADATA
    source_snapshot_id: str = ""
    table_name: str | None = None
    column_name: str | None = None
    sql_asset_id: str | None = None
    extracted_fact: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)
    deterministic: bool = True
    llm_generated: bool = False
    evidence_hash: str = ""
    source: str = ""
    detail: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_evidence(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "source_type" in value:
            return value
        migrated = dict(value)
        legacy = str(migrated.get("source", "")).lower()
        mapping = {
            "metadata": EvidenceSourceType.TABLE_METADATA,
            "table-role": EvidenceSourceType.TABLE_METADATA,
            "primary-key": EvidenceSourceType.PRIMARY_KEY,
            "foreign-key": EvidenceSourceType.FOREIGN_KEY,
            "field-profile": EvidenceSourceType.FIELD_PROFILE,
            "historical-sql": EvidenceSourceType.CERTIFIED_HISTORICAL_SQL,
            "llm": EvidenceSourceType.LLM_SEMANTIC_SUGGESTION,
        }
        migrated["source_type"] = mapping.get(legacy, legacy.upper() or "TABLE_METADATA")
        migrated["extracted_fact"] = migrated.get("detail", "")
        migrated["llm_generated"] = legacy == "llm"
        migrated["deterministic"] = legacy != "llm"
        return migrated

    @model_validator(mode="after")
    def synchronize_compatibility_fields(self) -> CandidateEvidence:
        if not self.source:
            self.source = str(self.source_type)
        if not self.detail:
            self.detail = self.extracted_fact
        if not self.extracted_fact:
            self.extracted_fact = self.detail
        if not self.evidence_id or not self.evidence_hash:
            payload = "|".join(
                [
                    str(self.source_type),
                    self.source_snapshot_id,
                    self.table_name or "",
                    self.column_name or "",
                    self.sql_asset_id or "",
                    self.extracted_fact,
                ]
            )
            digest = hashlib.sha256(payload.encode()).hexdigest()
            self.evidence_id = self.evidence_id or f"evidence-{digest[:24]}"
            self.evidence_hash = self.evidence_hash or digest
        return self


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
    sensitive_suggestion: bool | None = None
    unit_suggestion: str | None = None
    warnings: list[str] = Field(default_factory=list)


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


class CandidateDimension(BaseModel):
    candidate_id: str
    dimension: DimensionDefinition
    time_grain_suggestion: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT


class MetricFilterSupport(BaseModel):
    predicate: PropertyFilterPredicate
    occurrence_count: int = Field(ge=1)
    sql_coverage_count: int = Field(ge=1)
    consistency_ratio: float = Field(ge=0, le=1)
    conflicting_values: list[str] = Field(default_factory=list)


class CandidateMetric(BaseModel):
    candidate_id: str
    metric: MetricDefinition
    source_fact_table: str
    review_state: Literal["READY", "NEEDS_REVIEW"] = "NEEDS_REVIEW"
    filter_support: list[MetricFilterSupport] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT


class ObjectCandidateSet(BaseModel):
    snapshot_id: str
    object_types: list[CandidateObjectType] = Field(default_factory=list)
    properties: list[CandidateProperty] = Field(default_factory=list)
    bindings: list[CandidateObjectBinding] = Field(default_factory=list)
    link_types: list[CandidateLinkType] = Field(default_factory=list)
    physical_joins: list[CandidatePhysicalJoin] = Field(default_factory=list)
    dimensions: list[CandidateDimension] = Field(default_factory=list)
    metrics: list[CandidateMetric] = Field(default_factory=list)
    excluded_tables: dict[str, str] = Field(default_factory=dict)


class CreateConstructionRunRequest(BaseModel):
    source_snapshot_id: str
    profiling_snapshot_hash: str = ""
    historical_sql_snapshot_hash: str = ""
    catalog_mode: CatalogMode = CatalogMode.RAW_METADATA
    construction_mode: ConstructionMode = ConstructionMode.STRICT_CONSTRUCTION
    evidence_mode: ConstructionEvidenceMode = (
        ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL
    )
    llm_mode: Literal["mock", "live"] = "mock"
    provider: str = "mock"
    model: str = "deterministic-rules-v1"
    temperature: float = Field(default=0, ge=0, le=2)
    random_seed: int = 20260715
    created_by: str = "ontology-constructor"


class OntologyConstructionRun(BaseModel):
    run_id: str
    status: ConstructionRunStatus = ConstructionRunStatus.CREATED
    source_snapshot_id: str
    source_snapshot_hash: str
    profiling_snapshot_hash: str
    historical_sql_snapshot_hash: str
    catalog_mode: CatalogMode
    construction_mode: ConstructionMode
    evidence_mode: ConstructionEvidenceMode
    llm_mode: Literal["mock", "live"]
    provider: str
    model: str
    temperature: float
    random_seed: int
    git_sha: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    excluded_tables: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    seed_accessed: bool = False
    fallback_used: bool = False
    legacy_ontology_accessed: bool = False
    created_by: str
    promoted_draft_id: str | None = None
    evaluation: dict[str, Any] | None = None


class ConstructionCandidate(BaseModel):
    candidate_id: str
    run_id: str
    resource_type: Literal[
        "object_type",
        "property",
        "binding",
        "link_type",
        "physical_join",
        "dimension",
        "metric",
    ]
    status: ConstructionCandidateStatus = ConstructionCandidateStatus.PENDING
    original_candidate: dict[str, Any]
    current_resource: dict[str, Any]
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    decision: CandidateReviewDecision | None = None
    comment: str = ""
    revision: int = 0
    merge_target_candidate_id: str | None = None


class ReviewConstructionCandidateRequest(BaseModel):
    decision: CandidateReviewDecision
    reviewer: str = Field(min_length=1, max_length=100)
    modified_resource: dict[str, Any] | None = None
    comment: str = Field(default="", max_length=2000)
    merge_target_candidate_id: str | None = None


class PromoteConstructionRunRequest(BaseModel):
    draft_name: str = "Reviewed data-source-driven ontology"
    actor: str = "ontology-reviewer"


class ConstructionEvaluationReport(BaseModel):
    run_id: str
    gold_hash: str
    valid_run: bool
    metrics: dict[str, float | int | bool | str | None]
    provenance: dict[str, Any]
    generated_at: datetime = Field(default_factory=utc_now)


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
