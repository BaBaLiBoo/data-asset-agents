"""Deterministic table-role classification used before object candidate generation."""

from data_asset_agents.ontology.models import TableAsset, TableMetadata

from .models import TableRole


class TableRoleClassifier:
    """Classify governed tables without asking an LLM to choose physical assets."""

    def classify(self, table: TableAsset) -> tuple[TableRole, list[str]]:
        name = table.name.lower()
        evidence = [f"name={table.name}", f"status={table.status}", f"grain={table.grain}"]
        if table.status == "DEPRECATED" or name.startswith(("legacy_", "old_", "deprecated_")):
            return TableRole.DEPRECATED, evidence
        if table.status in {"TEMPORARY", "TEST"} or name.startswith(("tmp_", "test_")):
            return TableRole.TECHNICAL, evidence
        if "summary" in table.tags or name.startswith("dws_"):
            return TableRole.AGGREGATE_VIEW, evidence
        if name.startswith("dim_") or "dimension" in table.tags:
            return TableRole.CANONICAL_OBJECT, evidence
        if name.startswith("dwd_") or {"fact", "detail"}.intersection(table.tags):
            return TableRole.EVENT, evidence
        return TableRole.TECHNICAL, [*evidence, "no governed object/event pattern"]

    def eligible_for_object(self, table: TableAsset) -> bool:
        return self.classify(table)[0] in {TableRole.CANONICAL_OBJECT, TableRole.EVENT}

    def classify_raw(self, table: TableMetadata) -> tuple[TableRole, list[str]]:
        """Classify from physical evidence only, without a governed/Gold table asset."""

        name = table.table_name.lower()
        evidence = [
            f"name={table.table_name}",
            f"primary_key={table.primary_key}",
            f"foreign_key_count={len(table.foreign_keys)}",
        ]
        if name.startswith(("legacy_", "old_", "deprecated_")):
            return TableRole.DEPRECATED, evidence
        if name.startswith(("tmp_", "temp_", "test_")):
            return TableRole.TECHNICAL, evidence
        if name.startswith(("dws_", "agg_")) or any(
            token in name for token in ("summary", "aggregate")
        ):
            return TableRole.AGGREGATE_VIEW, evidence
        if name.startswith(("dim_", "master_")):
            return TableRole.CANONICAL_OBJECT, evidence
        if name.startswith(("dwd_", "fact_", "event_")):
            return TableRole.EVENT, evidence
        # Unknown physical tables remain review-only technical candidates. This avoids
        # silently asserting that every table is a business object.
        return TableRole.TECHNICAL, [*evidence, "no deterministic object/event evidence"]
