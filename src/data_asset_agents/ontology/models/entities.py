from typing import Literal

from pydantic import BaseModel, Field, model_validator


class BusinessConcept(BaseModel):
    id: str
    name: str
    kind: Literal["object", "metric", "dimension", "term"]
    description: str
    synonyms: list[str] = Field(default_factory=list)


class Metric(BaseModel):
    id: str
    name: str
    description: str
    expression: str
    base_table: str
    required_filters: dict[str, str] = Field(default_factory=dict)
    supported_dimensions: list[str] = Field(default_factory=list)
    time_dimension: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class Dimension(BaseModel):
    id: str
    name: str
    description: str
    table: str
    column: str
    synonyms: list[str] = Field(default_factory=list)


class PhysicalMapping(BaseModel):
    concept_id: str
    table: str
    columns: list[str]
    condition: str | None = None


class JoinDefinition(BaseModel):
    left_table: str
    right_table: str
    left_column: str
    right_column: str
    relationship: str = "many_to_one"
    enabled: bool = True

    @property
    def expression(self) -> str:
        return (
            f"{self.left_table}.{self.left_column} = "
            f"{self.right_table}.{self.right_column}"
        )


class TableAsset(BaseModel):
    name: str
    description: str
    status: Literal["ACTIVE", "DEPRECATED", "TEMPORARY", "TEST"]
    selectable: bool
    grain: str
    columns: list[str] = Field(default_factory=list)
    replacement: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def deprecated_assets_have_replacement(self) -> "TableAsset":
        if self.status == "DEPRECATED" and not self.replacement:
            raise ValueError("deprecated tables must define replacement")
        return self


class OntologyBundle(BaseModel):
    domain: dict[str, str]
    concepts: list[BusinessConcept]
    metrics: list[Metric]
    dimensions: list[Dimension]
    mappings: list[PhysicalMapping]
    joins: list[JoinDefinition]
    tables: list[TableAsset]
    policies: dict[str, object]
    glossary: dict[str, list[str]]
