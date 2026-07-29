from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ComparisonMode(StrEnum):
    KEYWORD_BASELINE = "KEYWORD_BASELINE"
    THREE_LAYER = "THREE_LAYER"


class Decision(StrEnum):
    DUPLICATE = "DUPLICATE"
    SUSPECTED_DUPLICATE = "SUSPECTED_DUPLICATE"
    NOT_DUPLICATE = "NOT_DUPLICATE"


class GoldLabel(StrEnum):
    DUPLICATE = "DUPLICATE"
    NOT_DUPLICATE = "NOT_DUPLICATE"


class DatasetSplit(StrEnum):
    DEV = "DEV"
    TEST = "TEST"


class ColumnMetadata(BaseModel):
    name: str
    cn_name: str | None = None
    comment: str | None = None
    data_type: str | None = None
    role: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("column name cannot be empty")
        return value


class TableMetadata(BaseModel):
    name: str
    cn_name: str | None = None
    comment: str | None = None
    columns: list[ColumnMetadata] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("table name cannot be empty")
        return value


class LineageInput(BaseModel):
    direct_upstreams: list[str] = Field(default_factory=list)
    root_sources: list[str] = Field(default_factory=list)
    column_signatures: list[str] = Field(default_factory=list)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    freshness: float = Field(default=0.0, ge=0.0, le=1.0)
    source_reliability: float = Field(default=0.0, ge=0.0, le=1.0)


class AssetInput(BaseModel):
    asset_id: str
    asset_name: str
    description: str
    business_domain: str
    sql_text: str
    sql_dialect: str = "spark"
    declared_grain: list[str]
    metric_name: str | None = None
    metric_definition: str | None = None
    tables: list[TableMetadata]
    lineage: LineageInput

    @field_validator(
        "asset_id",
        "asset_name",
        "description",
        "business_domain",
        "sql_text",
        "sql_dialect",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required text field cannot be empty")
        return value

    @field_validator("declared_grain")
    @classmethod
    def validate_grain(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("declared_grain cannot be empty")
        return cleaned

    @field_validator("tables")
    @classmethod
    def validate_tables(cls, value: list[TableMetadata]) -> list[TableMetadata]:
        if not value:
            raise ValueError("tables cannot be empty")
        return value


class LayerScore(BaseModel):
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    quality: float = Field(default=0.0, ge=0.0, le=1.0)
    available: bool = False
    components: dict[str, float | None] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_availability(self) -> "LayerScore":
        if self.available and self.score is None:
            raise ValueError("available layer must have a score")
        if not self.available and self.score is not None:
            raise ValueError("unavailable layer must not have a score")
        return self


class ComparisonRequest(BaseModel):
    mode: ComparisonMode
    asset_a: AssetInput
    asset_b: AssetInput


class ComparisonResult(BaseModel):
    mode: ComparisonMode
    decision: Decision
    keyword_score: float | None = Field(default=None, ge=0.0, le=1.0)
    semantic: LayerScore | None = None
    logic: LayerScore | None = None
    lineage: LayerScore | None = None
    fusion_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    threshold_profile: dict[str, float] = Field(default_factory=dict)
    explanation: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode_output(self) -> "ComparisonResult":
        if self.mode == ComparisonMode.KEYWORD_BASELINE:
            if self.keyword_score is None:
                raise ValueError("keyword mode must return keyword_score")
            if any(item is not None for item in (self.semantic, self.logic, self.lineage)):
                raise ValueError("keyword mode cannot return three-layer scores")
        if self.mode == ComparisonMode.THREE_LAYER:
            if self.keyword_score is not None:
                raise ValueError("three-layer mode cannot return keyword_score")
            if any(item is None for item in (self.semantic, self.logic, self.lineage)):
                raise ValueError("three-layer mode must return all layer results")
        return self


class AssetBatchImportRequest(BaseModel):
    assets: list[AssetInput] = Field(min_length=1, max_length=10_000)
    replace_existing: bool = True


class AssetImportResult(BaseModel):
    imported_count: int = Field(ge=0)
    catalog_size: int = Field(ge=0)
    asset_ids: list[str] = Field(default_factory=list)


class AssetListResult(BaseModel):
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    assets: list[AssetInput] = Field(default_factory=list)


class SearchDuplicatesRequest(BaseModel):
    asset: AssetInput | None = None
    asset_id: str | None = None
    candidate_top_k: int = Field(default=20, ge=1, le=200)
    result_top_k: int = Field(default=10, ge=1, le=100)
    business_domain_only: bool = True
    include_not_duplicate: bool = False

    @model_validator(mode="after")
    def validate_query_source(self) -> "SearchDuplicatesRequest":
        if (self.asset is None) == (self.asset_id is None):
            raise ValueError("exactly one of asset or asset_id must be supplied")
        if self.result_top_k > self.candidate_top_k:
            raise ValueError("result_top_k cannot exceed candidate_top_k")
        return self


class CandidateRecall(BaseModel):
    asset_id: str
    asset_name: str
    recall_score: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)


class DuplicateSearchItem(BaseModel):
    candidate: CandidateRecall
    comparison: ComparisonResult


class SearchDuplicatesResult(BaseModel):
    query_asset_id: str
    query_asset_name: str
    catalog_size: int = Field(ge=0)
    eligible_candidates: int = Field(ge=0)
    retrieved_candidates: int = Field(ge=0)
    returned_candidates: int = Field(ge=0)
    results: list[DuplicateSearchItem] = Field(default_factory=list)


class TextRetrievalMode(StrEnum):
    TEXT_ONLY = "TEXT_ONLY"
    TEXT_SQL_ENHANCED = "TEXT_SQL_ENHANCED"
    TEXT_ONLY_FALLBACK = "TEXT_ONLY_FALLBACK"


class TextSearchStatus(StrEnum):
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"


class TextAssetSearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "我想开发个人客户月均存款，看看有没有已有资产可以复用",
                    "top_k": 5,
                },
                {
                    "query": "我想开发个人客户月均存款",
                    "business_domain": "deposit",
                    "sql_text": (
                        "SELECT client_id, AVG(balance) "
                        "FROM dwd_customer_deposit GROUP BY client_id"
                    ),
                    "sql_dialect": "spark",
                    "top_k": 5,
                },
            ]
        }
    )

    query: str = Field(min_length=1)
    business_domain: str | None = None
    sql_text: str | None = None
    sql_dialect: str = "spark"
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query", "sql_dialect")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("business_domain", "sql_text")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class TextSearchCandidate(BaseModel):
    asset_id: str
    asset_name: str
    description: str
    business_domain: str
    recall_score: float = Field(ge=0.0, le=1.0)
    score_components: dict[str, float] = Field(default_factory=dict)


class TextAssetSearchResult(BaseModel):
    status: TextSearchStatus
    retrieval_mode: TextRetrievalMode
    original_query: str
    normalized_query: str
    normalization_method: str
    business_domain_applied: str | None
    catalog_size: int = Field(ge=0)
    eligible_assets: int = Field(ge=0)
    min_score: float = Field(ge=0.0, le=1.0)
    returned_candidates: int = Field(ge=0)
    candidates: list[TextSearchCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FullScanRequest(BaseModel):
    candidate_top_k: int = Field(default=20, ge=1, le=200)
    business_domain_only: bool = True
    include_suspected: bool = True
    include_not_duplicate: bool = False
    max_assets: int | None = Field(default=None, ge=2, le=10_000)


class FullScanPair(BaseModel):
    asset_a_id: str
    asset_a_name: str
    asset_b_id: str
    asset_b_name: str
    recall_score: float = Field(ge=0.0, le=1.0)
    recall_components: dict[str, float] = Field(default_factory=dict)
    comparison: ComparisonResult


class FullScanResult(BaseModel):
    catalog_size: int = Field(ge=0)
    scanned_assets: int = Field(ge=0)
    naive_pair_count: int = Field(ge=0)
    candidate_pair_count: int = Field(ge=0)
    compared_pair_count: int = Field(ge=0)
    comparison_reduction_rate: float = Field(ge=0.0, le=1.0)
    duplicate_count: int = Field(ge=0)
    suspected_count: int = Field(ge=0)
    returned_pair_count: int = Field(ge=0)
    pairs: list[FullScanPair] = Field(default_factory=list)


class EvaluationPair(BaseModel):
    pair_id: str
    family_id: str
    asset_a_id: str
    asset_b_id: str
    label: GoldLabel
    scenario: str
    split: DatasetSplit
