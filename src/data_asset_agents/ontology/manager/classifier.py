"""Deterministic table-role classification used before object candidate generation."""

from data_asset_agents.ontology.models import TableAsset

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
