"""Independent evaluation for ontology construction quality and review cost."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from data_asset_agents.ontology.manager.models import (
    ConstructionCandidate,
    ConstructionCandidateStatus,
    ConstructionEvaluationReport,
    DraftResources,
    OntologyConstructionRun,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _prf(predicted: set[str], gold: set[str]) -> tuple[float, float, float]:
    true_positive = len(predicted & gold)
    precision = _ratio(true_positive, len(predicted))
    recall = _ratio(true_positive, len(gold))
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


class GoldOntologyLoader:
    """Read-only Gold boundary. Construction generation never receives this loader."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[DraftResources, str]:
        resources = ObjectOntologySeedRepository({"gold": self.path}).load("gold")
        payload = resources.model_dump_json(exclude_none=True)
        return resources, hashlib.sha256(payload.encode()).hexdigest()


class OntologyConstructionEvaluator:
    def evaluate(
        self,
        run: OntologyConstructionRun,
        resources: DraftResources,
        candidates: list[ConstructionCandidate],
        gold: DraftResources,
        gold_hash: str,
    ) -> ConstructionEvaluationReport:
        metrics: dict[str, float | int | bool | str | None] = {}
        self._resource_prf(metrics, "object", resources.object_types, gold.object_types)
        self._resource_prf(metrics, "link", resources.link_types, gold.link_types)
        self._resource_prf(metrics, "dimension", resources.dimensions, gold.dimensions)
        self._resource_prf(metrics, "metric", resources.metrics, gold.metrics)

        predicted_objects = {item.id: item for item in resources.object_types}
        gold_objects = {item.id: item for item in gold.object_types}
        common_objects = predicted_objects.keys() & gold_objects.keys()
        metrics["object_boundary_match"] = _ratio(
            sum(
                predicted_objects[key].name == gold_objects[key].name
                and set(predicted_objects[key].property_ids)
                == set(gold_objects[key].property_ids)
                for key in common_objects
            ),
            len(common_objects),
        )
        metrics["object_primary_key_match"] = _ratio(
            sum(
                predicted_objects[key].primary_key_property_id
                == gold_objects[key].primary_key_property_id
                for key in common_objects
            ),
            len(common_objects),
        )

        predicted_properties = {item.id: item for item in resources.properties}
        gold_properties = {item.id: item for item in gold.properties}
        common_properties = predicted_properties.keys() & gold_properties.keys()
        metrics["property_coverage"] = _ratio(
            len(common_properties), len(gold_properties)
        )
        metrics["property_object_ownership_accuracy"] = _field_accuracy(
            predicted_properties, gold_properties, "object_type_id"
        )
        metrics["property_data_type_accuracy"] = _field_accuracy(
            predicted_properties, gold_properties, "data_type"
        )
        metrics["property_semantic_role_accuracy"] = _field_accuracy(
            predicted_properties, gold_properties, "semantic_role"
        )
        metrics["property_sensitive_suggestion_accuracy"] = _field_accuracy(
            predicted_properties, gold_properties, "sensitive"
        )
        metrics["property_synonym_match"] = _set_field_accuracy(
            predicted_properties, gold_properties, "synonyms"
        )

        predicted_bindings = {item.id: item for item in resources.bindings}
        gold_bindings = {item.id: item for item in gold.bindings}
        metrics["object_table_binding_accuracy"] = _field_accuracy(
            predicted_bindings, gold_bindings, "table_name"
        )
        metrics["property_column_binding_accuracy"] = _mapping_accuracy(
            predicted_bindings, gold_bindings, "property_bindings"
        )
        metrics["primary_key_binding_accuracy"] = _field_accuracy(
            predicted_bindings, gold_bindings, "primary_key_column"
        )

        predicted_joins = {item.id: item for item in resources.physical_joins}
        gold_joins = {item.id: item for item in gold.physical_joins}
        self._resource_prf(
            metrics, "physical_join", resources.physical_joins, gold.physical_joins
        )
        metrics["join_endpoint_accuracy"] = _multi_field_accuracy(
            predicted_joins,
            gold_joins,
            ("left_table", "left_column", "right_table", "right_column"),
        )
        metrics["join_cardinality_accuracy"] = _field_accuracy(
            predicted_joins, gold_joins, "cardinality"
        )
        metrics["physical_join_accuracy"] = metrics["join_endpoint_accuracy"]

        predicted_dimensions = {item.id: item for item in resources.dimensions}
        gold_dimensions = {item.id: item for item in gold.dimensions}
        metrics["dimension_property_reference_accuracy"] = _field_accuracy(
            predicted_dimensions, gold_dimensions, "property_id"
        )
        metrics["time_dimension_accuracy"] = _ratio(
            sum(
                predicted_dimensions[key].property_id
                == gold_dimensions[key].property_id
                for key in predicted_dimensions.keys() & gold_dimensions.keys()
                if "time" in key or "date" in key
            ),
            sum("time" in key or "date" in key for key in gold_dimensions),
        )

        predicted_metrics = {item.id: item for item in resources.metrics}
        gold_metrics = {item.id: item for item in gold.metrics}
        metrics["metric_measure_property_accuracy"] = _field_accuracy(
            predicted_metrics, gold_metrics, "measure_property_id"
        )
        metrics["metric_aggregation_accuracy"] = _field_accuracy(
            predicted_metrics, gold_metrics, "aggregation"
        )
        metrics["metric_fixed_filter_exact_match"] = _set_field_accuracy(
            predicted_metrics, gold_metrics, "filter_predicates", serialize=True
        )
        metrics["metric_time_property_accuracy"] = _field_accuracy(
            predicted_metrics, gold_metrics, "time_property_id"
        )
        metrics["metric_supported_dimensions_accuracy"] = _set_field_accuracy(
            predicted_metrics, gold_metrics, "supported_dimension_ids"
        )

        reviewed = [
            item
            for item in candidates
            if item.status != ConstructionCandidateStatus.PENDING
        ]
        metrics["reviewed_candidate_count"] = len(reviewed)
        for label, status in (
            ("direct_acceptance_rate", ConstructionCandidateStatus.ACCEPTED),
            ("modification_rate", ConstructionCandidateStatus.MODIFIED),
            ("rejection_rate", ConstructionCandidateStatus.REJECTED),
            ("merge_rate", ConstructionCandidateStatus.MERGED),
        ):
            metrics[label] = _ratio(
                sum(item.status == status for item in reviewed), len(reviewed)
            )
        metrics["edited_field_count"] = sum(
            _changed_fields(item.original_candidate, item.current_resource)
            for item in reviewed
            if item.status == ConstructionCandidateStatus.MODIFIED
        )
        metrics["strict_validation_passed"] = run.strict_validation_passed
        metrics["publication_succeeded"] = run.publication_succeeded
        metrics["runtime_activation_succeeded"] = (
            run.runtime_activation_succeeded
        )
        metrics["fallback_used"] = run.fallback_used
        metrics["seed_accessed"] = run.seed_accessed
        metrics["legacy_ontology_accessed"] = run.legacy_ontology_accessed
        valid_run = not (
            run.seed_accessed or run.fallback_used or run.legacy_ontology_accessed
        )
        return ConstructionEvaluationReport(
            run_id=run.run_id,
            gold_hash=gold_hash,
            valid_run=valid_run,
            metrics=metrics,
            provenance={
                "git_sha": run.git_sha,
                "source_snapshot_hash": run.source_snapshot_hash,
                "profiling_snapshot_hash": run.profiling_snapshot_hash,
                "historical_sql_snapshot_hash": run.historical_sql_snapshot_hash,
                "provider": run.provider,
                "model": run.model,
                "temperature": run.temperature,
                "random_seed": run.random_seed,
                "construction_mode": run.construction_mode,
                "evidence_mode": run.evidence_mode,
                "catalog_mode": run.catalog_mode,
            },
        )

    @staticmethod
    def _resource_prf(
        metrics: dict[str, Any], prefix: str, predicted: list[Any], gold: list[Any]
    ) -> None:
        precision, recall, f1 = _prf(
            {item.id for item in predicted}, {item.id for item in gold}
        )
        metrics[f"{prefix}_precision"] = precision
        metrics[f"{prefix}_recall"] = recall
        metrics[f"{prefix}_f1"] = f1


def _field_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float:
    common = predicted.keys() & gold.keys()
    return _ratio(
        sum(getattr(predicted[key], field) == getattr(gold[key], field) for key in common),
        len(common),
    )


def _multi_field_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], fields: tuple[str, ...]
) -> float:
    common = predicted.keys() & gold.keys()
    return _ratio(
        sum(
            all(
                getattr(predicted[key], field) == getattr(gold[key], field)
                for field in fields
            )
            for key in common
        ),
        len(common),
    )


def _set_field_accuracy(
    predicted: dict[str, Any],
    gold: dict[str, Any],
    field: str,
    *,
    serialize: bool = False,
) -> float:
    common = predicted.keys() & gold.keys()

    def normalized(value: Any) -> set[str]:
        items = list(value)
        if serialize:
            return {
                json.dumps(
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item,
                    sort_keys=True,
                )
                for item in items
            }
        return {str(item) for item in items}

    return _ratio(
        sum(
            normalized(getattr(predicted[key], field))
            == normalized(getattr(gold[key], field))
            for key in common
        ),
        len(common),
    )


def _mapping_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float:
    common = predicted.keys() & gold.keys()
    total = sum(len(getattr(gold[key], field)) for key in common)
    correct = sum(
        sum(
            getattr(predicted[key], field).get(item_key) == value
            for item_key, value in getattr(gold[key], field).items()
        )
        for key in common
    )
    return _ratio(correct, total)


def _changed_fields(original: dict[str, Any], current: dict[str, Any]) -> int:
    original_resource = next(
        (value for value in original.values() if isinstance(value, dict) and "id" in value),
        {},
    )
    return sum(
        original_resource.get(key) != value for key, value in current.items()
    )
