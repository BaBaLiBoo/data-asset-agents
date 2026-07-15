from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ReviewStatus(StrEnum):
    """Lifecycle shared by every machine-generated ontology candidate."""

    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class ValueFrequency(BaseModel):
    value: str
    count: int = Field(ge=0)


class ColumnProfile(BaseModel):
    """Bounded, masked statistics for one physical column."""

    table_name: str
    column_name: str
    data_type: str
    row_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    null_rate: float = Field(ge=0, le=1)
    distinct_count: int = Field(ge=0)
    unique_rate: float = Field(ge=0, le=1)
    minimum: str | None = None
    maximum: str | None = None
    top_values: list[ValueFrequency] = Field(default_factory=list)
    sample_values: list[str] = Field(default_factory=list)
    masked: bool = True


class ColumnReference(BaseModel):
    table: str | None = None
    column: str


class ColumnMetadata(BaseModel):
    name: str
    data_type: str
    nullable: bool
    comment: str | None = None
    default: str | None = None


class ForeignKeyMetadata(BaseModel):
    name: str | None = None
    constrained_columns: list[str]
    referred_schema: str | None = None
    referred_table: str
    referred_columns: list[str]


class IndexMetadata(BaseModel):
    name: str
    columns: list[str]
    unique: bool = False


class TableMetadata(BaseModel):
    schema_name: str
    table_name: str
    comment: str | None = None
    columns: list[ColumnMetadata]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyMetadata] = Field(default_factory=list)
    indexes: list[IndexMetadata] = Field(default_factory=list)
    profiles: list[ColumnProfile] = Field(default_factory=list)


class MetadataSnapshot(BaseModel):
    """Immutable physical metadata and profiling snapshot used as build evidence."""

    id: str = Field(default_factory=lambda: _identifier("snapshot"))
    schema_name: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = "postgresql"
    tables: list[TableMetadata]


class ParsedJoin(BaseModel):
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    expression: str


class ParsedAggregate(BaseModel):
    function: str
    expression: str
    alias: str | None = None


class HistoricalSQLAnalysis(BaseModel):
    """Structured SQLGlot output persisted alongside the original SQL asset."""

    id: str = Field(default_factory=lambda: _identifier("sql"))
    question: str | None = None
    sql_text: str
    certified: bool = False
    tables: list[str] = Field(default_factory=list)
    columns: list[ColumnReference] = Field(default_factory=list)
    joins: list[ParsedJoin] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    aggregates: list[ParsedAggregate] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    time_fields: list[ColumnReference] = Field(default_factory=list)


class UsageCount(BaseModel):
    key: str
    count: int = Field(ge=0)


class HistoricalSQLSummary(BaseModel):
    parsed_count: int = Field(ge=0)
    table_cooccurrence: list[UsageCount] = Field(default_factory=list)
    column_usage: list[UsageCount] = Field(default_factory=list)
    join_usage: list[UsageCount] = Field(default_factory=list)


class CandidateSemanticOutput(BaseModel):
    """Provider-neutral LangChain Structured Output for one candidate concept."""

    business_name: str
    business_object: str
    semantic_property: str
    role: Literal[
        "identifier",
        "measure",
        "dimension",
        "time",
        "status",
        "attribute",
    ]
    unit: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class CandidateConcept(CandidateSemanticOutput):
    id: str = Field(default_factory=lambda: _identifier("concept"))
    snapshot_id: str
    table_name: str
    column_name: str
    semantic_id: str
    status: ReviewStatus = ReviewStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CandidateMapping(BaseModel):
    id: str = Field(default_factory=lambda: _identifier("mapping"))
    snapshot_id: str
    candidate_concept_id: str
    concept_id: str
    table_name: str
    columns: list[str]
    column_bindings: dict[str, str] = Field(default_factory=dict)
    condition: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def physical_bindings(self) -> dict[str, str]:
        if self.column_bindings:
            return self.column_bindings
        if self.concept_id.startswith("dimension:") and len(self.columns) == 1:
            return {"value": self.columns[0]}
        return {column: column for column in self.columns}


class CandidateJoin(BaseModel):
    id: str = Field(default_factory=lambda: _identifier("join"))
    snapshot_id: str
    left_table: str
    right_table: str
    left_column: str
    right_column: str
    relationship: str = "many_to_one"
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


CandidatePayload = CandidateConcept | CandidateMapping | CandidateJoin


class CandidateEnvelope(BaseModel):
    id: str
    candidate_type: Literal["concept", "mapping", "join"]
    status: ReviewStatus
    payload: dict[str, Any]
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    review_note: str | None = None


class OntologyVersion(BaseModel):
    id: str = Field(default_factory=lambda: _identifier("version"))
    version: str
    description: str = ""
    source_snapshot_id: str | None = None
    status: Literal["PUBLISHED"] = "PUBLISHED"
    concept_count: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    join_count: int = Field(ge=0)
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_by: str
    is_current: bool = True


class OntologyBuildRequest(BaseModel):
    schema_name: str = Field(default="public", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    tables: list[str] | None = None
    sample_limit: int = Field(default=5, ge=1, le=10)
    top_value_limit: int = Field(default=5, ge=1, le=20)


class OntologyBuildResult(BaseModel):
    snapshot: MetadataSnapshot
    historical_sql: list[HistoricalSQLAnalysis]
    historical_summary: HistoricalSQLSummary
    concepts: list[CandidateConcept]
    mappings: list[CandidateMapping]
    joins: list[CandidateJoin]


class CandidateReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    edits: dict[str, Any] = Field(default_factory=dict)


class OntologyPublishRequest(BaseModel):
    version: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = Field(default="", max_length=1000)
    published_by: str = Field(min_length=1, max_length=100)
    snapshot_id: str | None = None


class ContractCheck(BaseModel):
    code: str
    passed: bool
    message: str
    candidate_id: str | None = None
    table: str | None = None
    column: str | None = None


class OntologyContractReport(BaseModel):
    valid: bool
    checks: list[ContractCheck] = Field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [check.message for check in self.checks if not check.passed]


class PublishDryRunReport(BaseModel):
    valid: bool
    version: str
    snapshot_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    contract: OntologyContractReport
    generated_sql: str | None = None
    sqlglot_valid: bool = False
    explain_passed: bool = False
    explain_plan: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
