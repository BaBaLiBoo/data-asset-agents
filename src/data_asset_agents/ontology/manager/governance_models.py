"""Strong contracts for ontology changes, drift, indexes, and object reads."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import utc_now


class BreakingLevel(StrEnum):
    NON_BREAKING = "NON_BREAKING"
    POTENTIALLY_BREAKING = "POTENTIALLY_BREAKING"
    BREAKING = "BREAKING"


class ResourceChange(BaseModel):
    resource_type: str
    resource_id: str
    change_type: Literal["ADDED", "MODIFIED", "DEPRECATED", "REMOVED"]
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    breaking_level: BreakingLevel
    reason: str


class OntologyChangeSet(BaseModel):
    draft_id: str
    base_version_id: str | None
    added_objects: list[ResourceChange] = Field(default_factory=list)
    modified_objects: list[ResourceChange] = Field(default_factory=list)
    deprecated_objects: list[ResourceChange] = Field(default_factory=list)
    removed_objects: list[ResourceChange] = Field(default_factory=list)
    added_properties: list[ResourceChange] = Field(default_factory=list)
    modified_properties: list[ResourceChange] = Field(default_factory=list)
    removed_properties: list[ResourceChange] = Field(default_factory=list)
    added_links: list[ResourceChange] = Field(default_factory=list)
    modified_links: list[ResourceChange] = Field(default_factory=list)
    removed_links: list[ResourceChange] = Field(default_factory=list)
    changed_bindings: list[ResourceChange] = Field(default_factory=list)
    changed_physical_joins: list[ResourceChange] = Field(default_factory=list)
    calculated_at: datetime = Field(default_factory=utc_now)

    @property
    def changes(self) -> list[ResourceChange]:
        return [
            *self.added_objects,
            *self.modified_objects,
            *self.deprecated_objects,
            *self.removed_objects,
            *self.added_properties,
            *self.modified_properties,
            *self.removed_properties,
            *self.added_links,
            *self.modified_links,
            *self.removed_links,
            *self.changed_bindings,
            *self.changed_physical_joins,
        ]


class OntologyImpactReport(BaseModel):
    draft_id: str
    affected_metrics: list[str] = Field(default_factory=list)
    affected_dimensions: list[str] = Field(default_factory=list)
    affected_physical_mappings: list[str] = Field(default_factory=list)
    affected_link_paths: list[str] = Field(default_factory=list)
    affected_sql_assets: list[str] = Field(default_factory=list)
    affected_benchmark_cases: list[str] = Field(default_factory=list)
    rebuild_concept_index: bool = False
    rebuild_sql_assets: bool = False
    rerun_gold_hashes: bool = False
    automatic_publish_allowed: bool = True
    breaking_changes: list[str] = Field(default_factory=list)
    calculated_at: datetime = Field(default_factory=utc_now)


class DriftSeverity(StrEnum):
    NONE = "NONE"
    ADDITIVE = "ADDITIVE"
    BREAKING = "BREAKING"


class BindingInspection(BaseModel):
    binding_id: str
    schema_hash: str
    columns: dict[str, str]
    primary_key_columns: list[str]
    inspected_at: datetime = Field(default_factory=utc_now)


class SchemaDriftReport(BaseModel):
    id: str
    binding_id: str
    ontology_version_id: str
    previous_schema_hash: str
    current_schema_hash: str
    added_columns: list[str] = Field(default_factory=list)
    removed_columns: list[str] = Field(default_factory=list)
    changed_types: dict[str, dict[str, str]] = Field(default_factory=dict)
    primary_key_changed: bool = False
    join_endpoints_changed: list[str] = Field(default_factory=list)
    affected_properties: list[str] = Field(default_factory=list)
    affected_links: list[str] = Field(default_factory=list)
    affected_metrics: list[str] = Field(default_factory=list)
    severity: DriftSeverity
    inspected_at: datetime = Field(default_factory=utc_now)


class SyncRunStatus(StrEnum):
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


class OntologySyncRun(BaseModel):
    run_id: str
    ontology_version_id: str
    status: SyncRunStatus = SyncRunStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error_message: str | None = None
    reports: list[SchemaDriftReport] = Field(default_factory=list)


class OntologyIndexType(StrEnum):
    BUSINESS_CONCEPT = "BUSINESS_CONCEPT"
    OBJECT_TYPE = "OBJECT_TYPE"
    PROPERTY = "PROPERTY"
    LINK_TYPE = "LINK_TYPE"


class OntologyIndexStatus(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"
    STALE = "STALE"


class OntologyIndexBuildRequest(BaseModel):
    index_type: OntologyIndexType = OntologyIndexType.BUSINESS_CONCEPT


class OntologyIndexBuild(BaseModel):
    build_id: str
    ontology_version_id: str
    index_type: OntologyIndexType
    status: OntologyIndexStatus
    source_hash: str
    embedding_model: str
    embedding_dimensions: int
    document_count: int = 0
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error_message: str | None = None
    is_current: bool = False


class DraftSemanticDryRunCase(BaseModel):
    benchmark_case_id: str
    question: str
    semantic_property_resolution: dict[str, str] = Field(default_factory=dict)
    physical_binding_resolution: dict[str, str] = Field(default_factory=dict)
    join_resolution: list[str] = Field(default_factory=list)
    generated_sql: str | None = None
    sqlglot_valid: bool = False
    ontology_policy_valid: bool = False
    explain_passed: bool = False
    errors: list[str] = Field(default_factory=list)


class ObjectFilterOperator(StrEnum):
    EQ = "EQ"
    IN = "IN"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"


class ObjectFilter(BaseModel):
    property_id: str
    operator: ObjectFilterOperator
    value: str | list[str]


class ObjectRecord(BaseModel):
    object_type_id: str
    primary_key: Any
    properties: dict[str, Any]
    available_links: list[str]
    ontology_version_id: str
