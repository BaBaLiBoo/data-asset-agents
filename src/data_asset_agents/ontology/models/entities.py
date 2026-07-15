from typing import Any, Literal

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
    """Deterministic semantic-role to physical-column binding.

    ``columns`` remains a read-compatible migration field. New code resolves
    fields exclusively through ``column_bindings`` and never by list position.
    """

    concept_id: str
    table: str
    column_bindings: dict[str, str] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    condition: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_columns(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("column_bindings"):
            return value
        migrated = dict(value)
        columns = list(migrated.get("columns") or [])
        concept_id = str(migrated.get("concept_id", ""))
        if concept_id.startswith("dimension:") and len(columns) == 1:
            migrated["column_bindings"] = {"value": columns[0]}
        elif concept_id.startswith("metric:"):
            bindings: dict[str, str] = {}
            for column in columns:
                lowered = column.lower()
                if "amount" in lowered:
                    bindings["amount"] = column
                elif lowered == "transaction_id":
                    bindings["transaction_id"] = column
                elif lowered == "customer_id":
                    bindings["customer_id"] = column
                elif "status" in lowered:
                    bindings["status"] = column
                elif lowered == "card_type":
                    bindings["card_type"] = column
                elif any(token in lowered for token in ("date", "time")):
                    bindings["event_time"] = column
                else:
                    bindings[column] = column
            migrated["column_bindings"] = bindings
        else:
            migrated["column_bindings"] = {column: column for column in columns}
        return migrated

    @model_validator(mode="after")
    def synchronize_compatibility_columns(self) -> "PhysicalMapping":
        if not self.columns:
            self.columns = list(dict.fromkeys(self.column_bindings.values()))
        return self

    def column_for(self, semantic_role: str) -> str | None:
        return self.column_bindings.get(semantic_role)


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
