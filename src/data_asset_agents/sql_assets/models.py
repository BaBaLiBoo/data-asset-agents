from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from data_asset_agents.ontology.models import ColumnReference, ParsedJoin
from data_asset_agents.text2sql.models import SemanticQuery


class CertificationLevel(StrEnum):
    NONE = "NONE"
    REVIEWED = "REVIEWED"
    CERTIFIED = "CERTIFIED"
    GOLD = "GOLD"


class SQLExecutionStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    EXPLAIN_PASSED = "EXPLAIN_PASSED"
    EXPLAIN_FAILED = "EXPLAIN_FAILED"
    PARSE_FAILED = "PARSE_FAILED"


class SQLAsset(BaseModel):
    """A certified historical query and its parsed, reviewable structure."""

    id: str
    question: str
    business_summary: str = ""
    certified: bool = False
    certification_level: CertificationLevel = CertificationLevel.NONE
    sql_text: str
    dialect: str = "postgres"
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[ColumnReference] = Field(default_factory=list)
    joins: list[ParsedJoin] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    limit: int | None = None
    ctes: list[str] = Field(default_factory=list)
    subquery_count: int = 0
    window_functions: list[str] = Field(default_factory=list)
    aggregates: list[str] = Field(default_factory=list)
    ast_node_types: dict[str, int] = Field(default_factory=dict)
    ast_fingerprint: str = ""
    structural_tags: list[str] = Field(default_factory=list)
    physical_mapping_ids: list[str] = Field(default_factory=list)
    invalid_columns: list[str] = Field(default_factory=list)
    unapproved_joins: list[str] = Field(default_factory=list)
    parse_error: str | None = None
    explain_error: str | None = None
    execution_status: SQLExecutionStatus = SQLExecutionStatus.NOT_CHECKED
    lifecycle_valid: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def retrieval_text(self) -> str:
        return " ".join(
            part
            for part in (
                self.question,
                self.business_summary,
                " ".join(self.metrics),
                " ".join(self.dimensions),
                " ".join(self.structural_tags),
            )
            if part
        )

    @property
    def hard_eligible(self) -> bool:
        return bool(
            self.certified
            and self.parse_error is None
            and self.lifecycle_valid
            and not self.invalid_columns
            and not self.unapproved_joins
            and self.execution_status == SQLExecutionStatus.EXPLAIN_PASSED
        )


class SQLAssetBuildRequest(BaseModel):
    source_path: str | None = None


class SQLAssetBuildReport(BaseModel):
    parsed: int = 0
    indexed: int = 0
    eligible: int = 0
    excluded: int = 0
    assets: list[SQLAsset] = Field(default_factory=list)


class SQLAssetSearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    semantic_query: SemanticQuery | None = None
    selected_tables: list[str] = Field(default_factory=list)
    selected_columns: dict[str, list[str]] = Field(default_factory=dict)
    join_conditions: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=50)


class SQLAssetScore(BaseModel):
    vector_similarity: float = 0.0
    metric_match: float = 0.0
    dimension_match: float = 0.0
    table_column_coverage: float = 0.0
    join_match: float = 0.0
    ast_similarity: float = 0.0
    certification_score: float = 0.0
    lifecycle_score: float = 0.0
    total: float = 0.0


class SQLAssetSearchResult(BaseModel):
    asset: SQLAsset
    score: SQLAssetScore
    evidence: list[str] = Field(default_factory=list)


class SQLRewriteResult(BaseModel):
    used_template: bool = False
    asset_id: str | None = None
    original_sql: str | None = None
    rewritten_sql: str | None = None
    fallback_reason: str | None = None
    changes: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    explain_plan: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
