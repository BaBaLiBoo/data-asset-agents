"""Independent two-stage evaluation for ontology construction quality and review cost."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from data_asset_agents.ontology.manager.models import (
    ConstructionCandidate,
    ConstructionCandidateStatus,
    ConstructionEvaluationReport,
    DimensionDefinition,
    DraftResources,
    LinkType,
    MetricDefinition,
    ObjectDataSourceBinding,
    ObjectType,
    OntologyConstructionRun,
    PhysicalJoinDefinition,
    PropertyDefinition,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)

RESOURCE_MODELS: dict[str, tuple[str, str, type[Any]]] = {
    "object_type": ("object_types", "object_type", ObjectType),
    "property": ("properties", "property", PropertyDefinition),
    "binding": ("bindings", "binding", ObjectDataSourceBinding),
    "link_type": ("link_types", "link_type", LinkType),
    "physical_join": ("physical_joins", "physical_join", PhysicalJoinDefinition),
    "dimension": ("dimensions", "dimension", DimensionDefinition),
    "metric": ("metrics", "metric", MetricDefinition),
}

STRUCTURAL_FIELDS = {
    "object_type_id",
    "property_ids",
    "primary_key_property_id",
    "title_property_id",
    "source_object_type_id",
    "target_object_type_id",
    "cardinality",
    "aggregation",
    "supported_dimension_ids",
    "filter_predicates",
    "measure_property_id",
    "time_property_id",
    "property_id",
}
SEMANTIC_FIELDS = {
    "name",
    "plural_name",
    "description",
    "synonyms",
    "unit",
    "source_role_name",
    "target_role_name",
}
PHYSICAL_FIELDS = {
    "table_name",
    "primary_key_column",
    "property_bindings",
    "left_table",
    "left_column",
    "right_table",
    "right_column",
    "schema_name",
}
TERMINAL_STATUSES = {
    ConstructionCandidateStatus.ACCEPTED,
    ConstructionCandidateStatus.MODIFIED,
    ConstructionCandidateStatus.REJECTED,
    ConstructionCandidateStatus.MERGED,
}


def _ratio(numerator: int, denominator: int) -> float | None:
    """Return an accuracy ratio, or None when no comparable observations exist."""

    return numerator / denominator if denominator else None


def _prf(
    predicted: set[Any], gold: set[Any]
) -> tuple[float | None, float | None, float | None]:
    """Standard PRF with undefined empty denominators represented as null."""

    true_positive = len(predicted & gold)
    precision = true_positive / len(predicted) if predicted else None
    recall = true_positive / len(gold) if gold else None
    if precision is None and recall is None:
        return None, None, None
    if precision is None or recall is None or precision + recall == 0:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


class GoldOntologyLoader:
    """Read-only Gold boundary. Construction generation never receives this loader."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[DraftResources, str]:
        resources = ObjectOntologySeedRepository({"gold": self.path}).load("gold")
        payload = resources.model_dump_json(exclude_none=True)
        return resources, hashlib.sha256(payload.encode()).hexdigest()


class OntologyConstructionEvaluator:
    """Score immutable generated candidates separately from reviewed Draft resources."""

    def raw_resources(
        self, candidates: list[ConstructionCandidate]
    ) -> DraftResources:
        resources = DraftResources()
        for candidate in candidates:
            collection, wrapper, model = RESOURCE_MODELS[candidate.resource_type]
            payload = candidate.original_candidate.get(wrapper)
            if not isinstance(payload, dict):
                # Older persisted rows may contain the resource directly.
                payload = candidate.current_resource
            getattr(resources, collection).append(model.model_validate(payload))
        return resources

    def evaluate_raw(
        self,
        run: OntologyConstructionRun,
        candidates: list[ConstructionCandidate],
        gold: DraftResources,
        gold_hash: str,
    ) -> ConstructionEvaluationReport:
        raw = self.raw_resources(candidates)
        raw_metrics, errors = self._quality_metrics(raw, gold, candidates=candidates)
        return ConstructionEvaluationReport(
            run_id=run.run_id,
            gold_hash=gold_hash,
            valid_run=not (
                run.seed_accessed or run.fallback_used or run.legacy_ontology_accessed
            ),
            metrics=raw_metrics,
            raw_candidate_metrics=raw_metrics,
            error_analysis={"raw_candidate_errors": errors},
            provenance=self._provenance(run, evaluation_stage="RAW_CANDIDATE"),
        )

    def evaluate(
        self,
        run: OntologyConstructionRun,
        resources: DraftResources,
        candidates: list[ConstructionCandidate],
        gold: DraftResources,
        gold_hash: str,
        *,
        raw_report: ConstructionEvaluationReport | None = None,
    ) -> ConstructionEvaluationReport:
        raw_metrics = (
            dict(raw_report.raw_candidate_metrics)
            if raw_report is not None
            else self._quality_metrics(
                self.raw_resources(candidates), gold, candidates=candidates
            )[0]
        )
        reviewed_metrics, reviewed_errors = self._quality_metrics(resources, gold)
        review_cost = self._review_cost(candidates, gold)
        lifecycle = {
            "strict_validation_passed": run.strict_validation_passed,
            "publication_succeeded": run.publication_succeeded,
            "runtime_activation_succeeded": run.runtime_activation_succeeded,
            "fallback_used": run.fallback_used,
            "seed_accessed": run.seed_accessed,
            "legacy_ontology_accessed": run.legacy_ontology_accessed,
        }
        reviewed_metrics.update(lifecycle)
        review_delta = self._review_delta(raw_metrics, reviewed_metrics)
        legacy_metrics = {**reviewed_metrics, **review_cost}
        valid_run = not (
            run.seed_accessed or run.fallback_used or run.legacy_ontology_accessed
        )
        return ConstructionEvaluationReport(
            run_id=run.run_id,
            gold_hash=gold_hash,
            valid_run=valid_run,
            metrics=legacy_metrics,
            raw_candidate_metrics=raw_metrics,
            reviewed_draft_metrics=reviewed_metrics,
            review_delta=review_delta,
            review_cost=review_cost,
            error_analysis={
                "raw_candidate_errors": self._quality_metrics(
                    self.raw_resources(candidates), gold, candidates=candidates
                )[1],
                "reviewed_draft_errors": reviewed_errors,
            },
            provenance=self._provenance(run, evaluation_stage="REVIEWED_DRAFT"),
        )

    def _quality_metrics(
        self,
        resources: DraftResources,
        gold: DraftResources,
        *,
        candidates: list[ConstructionCandidate] | None = None,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        metrics: dict[str, Any] = {}
        errors: Counter[str] = Counter()
        for prefix, predicted, expected in (
            ("object", resources.object_types, gold.object_types),
            ("property", resources.properties, gold.properties),
            ("binding", resources.bindings, gold.bindings),
            ("physical_join", resources.physical_joins, gold.physical_joins),
            ("link", resources.link_types, gold.link_types),
            ("dimension", resources.dimensions, gold.dimensions),
            ("metric", resources.metrics, gold.metrics),
        ):
            self._resource_prf(metrics, prefix, predicted, expected)
            predicted_ids = {item.id for item in predicted}
            gold_ids = {item.id for item in expected}
            errors[f"{prefix}_missing"] += len(gold_ids - predicted_ids)
            errors[f"{prefix}_extra"] += len(predicted_ids - gold_ids)

        predicted_objects = _index(resources.object_types)
        gold_objects = _index(gold.object_types)
        metrics.update(
            {
                "object_id_match": _id_coverage(predicted_objects, gold_objects),
                "object_name_match": _field_accuracy(
                    predicted_objects, gold_objects, "name"
                ),
                "object_boundary_description_similarity": _text_field_similarity(
                    predicted_objects, gold_objects, "description"
                ),
                "object_primary_key_match": _field_accuracy(
                    predicted_objects, gold_objects, "primary_key_property_id"
                ),
                "object_title_property_match": _field_accuracy(
                    predicted_objects, gold_objects, "title_property_id"
                ),
                "object_property_coverage": _set_overlap_accuracy(
                    predicted_objects, gold_objects, "property_ids"
                ),
            }
        )
        predicted_properties = _index(resources.properties)
        gold_properties = _index(gold.properties)
        metrics.update(
            {
                "property_object_ownership_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "object_type_id"
                ),
                "property_data_type_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "data_type"
                ),
                "property_semantic_role_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "semantic_role"
                ),
                "property_nullable_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "nullable"
                ),
                "property_filterable_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "filterable"
                ),
                "property_groupable_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "groupable"
                ),
                "property_sensitive_suggestion_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "sensitive"
                ),
                "property_unit_accuracy": _field_accuracy(
                    predicted_properties, gold_properties, "unit"
                ),
                "property_synonym_overlap": _set_overlap_accuracy(
                    predicted_properties, gold_properties, "synonyms"
                ),
            }
        )
        for key in predicted_properties.keys() & gold_properties.keys():
            if (
                predicted_properties[key].semantic_role
                != gold_properties[key].semantic_role
            ):
                errors["property_role_wrong"] += 1

        predicted_bindings = _index(resources.bindings)
        gold_bindings = _index(gold.bindings)
        metrics.update(
            {
                "object_table_binding_accuracy": _field_accuracy(
                    predicted_bindings, gold_bindings, "table_name"
                ),
                "property_column_binding_accuracy": _mapping_accuracy(
                    predicted_bindings, gold_bindings, "property_bindings"
                ),
                "primary_key_binding_accuracy": _field_accuracy(
                    predicted_bindings, gold_bindings, "primary_key_column"
                ),
                "snapshot_consistency": _binding_snapshot_consistency(
                    predicted_bindings
                ),
                "binding_coverage": _id_coverage(predicted_bindings, gold_bindings),
            }
        )
        for key in predicted_bindings.keys() & gold_bindings.keys():
            if any(
                getattr(predicted_bindings[key], field)
                != getattr(gold_bindings[key], field)
                for field in (
                    "table_name",
                    "primary_key_column",
                    "property_bindings",
                )
            ):
                errors["binding_wrong"] += 1

        predicted_joins = _index(resources.physical_joins)
        gold_joins = _index(gold.physical_joins)
        metrics.update(
            {
                "join_left_table_match": _field_accuracy(
                    predicted_joins, gold_joins, "left_table"
                ),
                "join_left_column_match": _field_accuracy(
                    predicted_joins, gold_joins, "left_column"
                ),
                "join_right_table_match": _field_accuracy(
                    predicted_joins, gold_joins, "right_table"
                ),
                "join_right_column_match": _field_accuracy(
                    predicted_joins, gold_joins, "right_column"
                ),
                "join_endpoint_accuracy": _multi_field_accuracy(
                    predicted_joins,
                    gold_joins,
                    ("left_table", "left_column", "right_table", "right_column"),
                ),
                "join_direction_independent_endpoint_match": _join_endpoint_prf(
                    resources.physical_joins, gold.physical_joins
                )[2],
                "join_cardinality_accuracy": _field_accuracy(
                    predicted_joins, gold_joins, "cardinality"
                ),
                "join_evidence_coverage": _candidate_evidence_coverage(
                    candidates, "physical_join"
                ),
            }
        )
        for key in predicted_joins.keys() & gold_joins.keys():
            if any(
                getattr(predicted_joins[key], field)
                != getattr(gold_joins[key], field)
                for field in (
                    "left_table",
                    "left_column",
                    "right_table",
                    "right_column",
                    "cardinality",
                )
            ):
                errors["join_wrong"] += 1

        link_metrics = _link_metrics(
            resources.link_types, gold.link_types, resources.physical_joins
        )
        metrics.update(link_metrics)
        predicted_links = _index(resources.link_types)
        gold_links = _index(gold.link_types)
        for key in predicted_links.keys() & gold_links.keys():
            predicted_link = predicted_links[key]
            gold_link = gold_links[key]
            if _canonical_link_pair(predicted_link) != _canonical_link_pair(gold_link):
                errors["link_endpoint_wrong"] += 1
            elif _directed_link(predicted_link) != _directed_link(gold_link):
                errors["link_direction_wrong"] += 1
            if predicted_link.name != gold_link.name:
                errors["link_name_wrong"] += 1
            if predicted_link.cardinality != gold_link.cardinality:
                errors["cardinality_wrong"] += 1

        predicted_dimensions = _index(resources.dimensions)
        gold_dimensions = _index(gold.dimensions)
        metrics.update(
            {
                "dimension_property_reference_accuracy": _field_accuracy(
                    predicted_dimensions, gold_dimensions, "property_id"
                ),
                "dimension_time_grain_accuracy": _dimension_time_accuracy(
                    predicted_dimensions, gold_dimensions
                ),
                "dimension_semantic_role_consistency": _dimension_role_consistency(
                    resources, predicted_dimensions
                ),
                "dimension_synonym_overlap": _set_overlap_accuracy(
                    predicted_dimensions, gold_dimensions, "synonyms"
                ),
            }
        )
        for key in predicted_dimensions.keys() & gold_dimensions.keys():
            if (
                predicted_dimensions[key].property_id
                != gold_dimensions[key].property_id
            ):
                errors["dimension_wrong"] += 1

        predicted_metrics = _index(resources.metrics)
        gold_metrics = _index(gold.metrics)
        metrics.update(
            {
                "metric_measure_property_accuracy": _field_accuracy(
                    predicted_metrics, gold_metrics, "measure_property_id"
                ),
                "metric_aggregation_accuracy": _field_accuracy(
                    predicted_metrics, gold_metrics, "aggregation"
                ),
                "metric_fixed_filter_exact_match": _set_field_accuracy(
                    predicted_metrics,
                    gold_metrics,
                    "filter_predicates",
                    serialize=True,
                ),
                "metric_filter_support_confidence": _metric_filter_support_confidence(
                    candidates
                ),
                "metric_time_property_accuracy": _field_accuracy(
                    predicted_metrics, gold_metrics, "time_property_id"
                ),
                "metric_supported_dimensions_accuracy": _set_overlap_accuracy(
                    predicted_metrics, gold_metrics, "supported_dimension_ids"
                ),
                "metric_source_sql_evidence_coverage": _candidate_evidence_coverage(
                    candidates, "metric", source_prefix="SQL_"
                ),
            }
        )
        for key in predicted_metrics.keys() & gold_metrics.keys():
            predicted_metric = predicted_metrics[key]
            gold_metric = gold_metrics[key]
            if predicted_metric.aggregation != gold_metric.aggregation:
                errors["metric_aggregation_wrong"] += 1
            if _normalized_set(
                predicted_metric.filter_predicates, serialize=True
            ) != _normalized_set(gold_metric.filter_predicates, serialize=True):
                errors["metric_filter_wrong"] += 1
            if predicted_metric.time_property_id != gold_metric.time_property_id:
                errors["metric_time_wrong"] += 1
            if set(predicted_metric.supported_dimension_ids) != set(
                gold_metric.supported_dimension_ids
            ):
                errors["supported_dimension_wrong"] += 1
        return metrics, {key: value for key, value in errors.items() if value}

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

    @staticmethod
    def _review_delta(
        raw: dict[str, Any], reviewed: dict[str, Any]
    ) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for key in raw.keys() & reviewed.keys():
            raw_value = raw[key]
            reviewed_value = reviewed[key]
            if (
                isinstance(raw_value, (int, float))
                and not isinstance(raw_value, bool)
                and isinstance(reviewed_value, (int, float))
                and not isinstance(reviewed_value, bool)
            ):
                result[key] = reviewed_value - raw_value
            elif raw_value is None or reviewed_value is None:
                result[key] = None
        return result

    @staticmethod
    def _review_cost(
        candidates: list[ConstructionCandidate], gold: DraftResources
    ) -> dict[str, Any]:
        reviewed = [item for item in candidates if item.status in TERMINAL_STATUSES]
        status_counts = Counter(item.status for item in reviewed)
        resource_type_edit_counts: Counter[str] = Counter()
        resource_type_rejection_counts: Counter[str] = Counter()
        category_counts: Counter[str] = Counter()
        total_edited_fields = 0
        retained_count = 0
        modified_count = status_counts[ConstructionCandidateStatus.MODIFIED]
        for item in reviewed:
            if item.status in {
                ConstructionCandidateStatus.ACCEPTED,
                ConstructionCandidateStatus.MODIFIED,
            }:
                retained_count += 1
            if item.status == ConstructionCandidateStatus.REJECTED:
                resource_type_rejection_counts[item.resource_type] += 1
            if item.status != ConstructionCandidateStatus.MODIFIED:
                continue
            changed = _changed_field_names(item.original_candidate, item.current_resource)
            total_edited_fields += len(changed)
            resource_type_edit_counts[item.resource_type] += len(changed)
            for field in changed:
                category_counts[_edit_category(field)] += 1
        manual = _manual_creation_cost(gold)
        review_operations = (
            len(reviewed)
            + total_edited_fields
            + status_counts[ConstructionCandidateStatus.MERGED]
        )
        manual_operations = manual["manual_creation_operations"]
        saving = (
            1 - review_operations / manual_operations if manual_operations else None
        )
        return {
            "review_candidate_count": len(reviewed),
            "accepted_without_change_count": status_counts[
                ConstructionCandidateStatus.ACCEPTED
            ],
            "modified_candidate_count": modified_count,
            "rejected_candidate_count": status_counts[
                ConstructionCandidateStatus.REJECTED
            ],
            "merged_candidate_count": status_counts[ConstructionCandidateStatus.MERGED],
            "deferred_candidate_count": sum(
                item.status == ConstructionCandidateStatus.DEFERRED
                for item in candidates
            ),
            "direct_acceptance_rate": _ratio(
                status_counts[ConstructionCandidateStatus.ACCEPTED], len(reviewed)
            ),
            "modification_rate": _ratio(modified_count, len(reviewed)),
            "rejection_rate": _ratio(
                status_counts[ConstructionCandidateStatus.REJECTED], len(reviewed)
            ),
            "merge_rate": _ratio(
                status_counts[ConstructionCandidateStatus.MERGED], len(reviewed)
            ),
            "total_edited_field_count": total_edited_fields,
            "edited_field_count": total_edited_fields,
            "average_edited_fields_per_modified_candidate": _ratio(
                total_edited_fields, modified_count
            ),
            "average_edited_fields_per_retained_candidate": _ratio(
                total_edited_fields, retained_count
            ),
            "resource_type_edit_counts": dict(resource_type_edit_counts),
            "resource_type_rejection_counts": dict(resource_type_rejection_counts),
            "structural_edit_count": category_counts["structural"],
            "semantic_edit_count": category_counts["semantic"],
            "physical_mapping_edit_count": category_counts["physical"],
            "estimated_review_operations": review_operations,
            "optional_review_duration_seconds": None,
            **manual,
            "review_saving_rate": saving,
            "cost_estimation_note": (
                "Engineering operation estimate: one decision plus each edited field; "
                "manual creation counts one create operation plus populated fields. "
                "It is not measured human time."
            ),
        }

    @staticmethod
    def _provenance(
        run: OntologyConstructionRun, *, evaluation_stage: str
    ) -> dict[str, Any]:
        return {
            "evaluation_stage": evaluation_stage,
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
            "llm_mode": run.llm_mode,
            "llm_invocations": run.llm_invocations,
        }


def _index(items: Iterable[Any]) -> dict[str, Any]:
    return {item.id: item for item in items}


def _id_coverage(predicted: dict[str, Any], gold: dict[str, Any]) -> float | None:
    return _ratio(len(predicted.keys() & gold.keys()), len(gold))


def _field_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float | None:
    common = predicted.keys() & gold.keys()
    return _ratio(
        sum(getattr(predicted[key], field) == getattr(gold[key], field) for key in common),
        len(common),
    )


def _multi_field_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], fields: tuple[str, ...]
) -> float | None:
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


def _normalized_set(value: Any, *, serialize: bool = False) -> set[str]:
    if value is None:
        return set()
    values = list(value)
    if serialize:
        return {
            json.dumps(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item,
                sort_keys=True,
            )
            for item in values
        }
    return {str(item).lower() for item in values}


def _set_field_accuracy(
    predicted: dict[str, Any],
    gold: dict[str, Any],
    field: str,
    *,
    serialize: bool = False,
) -> float | None:
    common = predicted.keys() & gold.keys()
    return _ratio(
        sum(
            _normalized_set(getattr(predicted[key], field), serialize=serialize)
            == _normalized_set(getattr(gold[key], field), serialize=serialize)
            for key in common
        ),
        len(common),
    )


def _set_overlap_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float | None:
    common = predicted.keys() & gold.keys()
    scores: list[float] = []
    for key in common:
        predicted_set = _normalized_set(getattr(predicted[key], field))
        gold_set = _normalized_set(getattr(gold[key], field))
        union = predicted_set | gold_set
        scores.append(len(predicted_set & gold_set) / len(union) if union else 1.0)
    return sum(scores) / len(scores) if scores else None


def _text_field_similarity(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float | None:
    common = predicted.keys() & gold.keys()
    scores = [
        SequenceMatcher(
            None,
            str(getattr(predicted[key], field)).strip().lower(),
            str(getattr(gold[key], field)).strip().lower(),
        ).ratio()
        for key in common
    ]
    return sum(scores) / len(scores) if scores else None


def _mapping_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any], field: str
) -> float | None:
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


def _binding_snapshot_consistency(bindings: dict[str, Any]) -> float | None:
    if not bindings:
        return None
    snapshot_ids = {
        item.latest_snapshot_id for item in bindings.values() if item.latest_snapshot_id
    }
    return 1.0 if len(snapshot_ids) <= 1 else 0.0


def _canonical_join_endpoint(join: PhysicalJoinDefinition) -> tuple[str, str]:
    return tuple(
        sorted(
            (
                f"{join.left_table}.{join.left_column}",
                f"{join.right_table}.{join.right_column}",
            )
        )
    )


def _join_endpoint_prf(
    predicted: list[PhysicalJoinDefinition], gold: list[PhysicalJoinDefinition]
) -> tuple[float | None, float | None, float | None]:
    return _prf(
        {_canonical_join_endpoint(item) for item in predicted},
        {_canonical_join_endpoint(item) for item in gold},
    )


def _canonical_link_pair(link: LinkType) -> tuple[str, str]:
    return tuple(
        sorted((link.source_object_type_id, link.target_object_type_id))
    )


def _directed_link(link: LinkType) -> tuple[str, str]:
    return link.source_object_type_id, link.target_object_type_id


def _semantic_link(link: LinkType) -> tuple[str, str, str, str]:
    return (
        link.source_object_type_id,
        link.target_object_type_id,
        link.id,
        link.name.strip().lower(),
    )


def _link_metrics(
    predicted: list[LinkType],
    gold: list[LinkType],
    physical_joins: list[PhysicalJoinDefinition],
) -> dict[str, Any]:
    endpoint_precision, endpoint_recall, endpoint_f1 = _prf(
        {_canonical_link_pair(item) for item in predicted},
        {_canonical_link_pair(item) for item in gold},
    )
    directed_precision, directed_recall, directed_f1 = _prf(
        {_directed_link(item) for item in predicted},
        {_directed_link(item) for item in gold},
    )
    semantic_precision, semantic_recall, semantic_f1 = _prf(
        {_semantic_link(item) for item in predicted},
        {_semantic_link(item) for item in gold},
    )
    predicted_by_id = _index(predicted)
    gold_by_id = _index(gold)
    known_joins = {item.id for item in physical_joins}
    consistency = _ratio(
        sum(bool(set(item.physical_join_ids) & known_joins) for item in predicted),
        len(predicted),
    )
    return {
        "link_endpoint_pair_precision": endpoint_precision,
        "link_endpoint_pair_recall": endpoint_recall,
        "link_endpoint_pair_f1": endpoint_f1,
        "link_directed_precision": directed_precision,
        "link_directed_recall": directed_recall,
        "directed_link_f1": directed_f1,
        "link_semantic_precision": semantic_precision,
        "link_semantic_recall": semantic_recall,
        "semantic_link_f1": semantic_f1,
        "link_endpoint_accuracy": _multi_field_accuracy(
            predicted_by_id,
            gold_by_id,
            ("source_object_type_id", "target_object_type_id"),
        ),
        "link_direction_accuracy": _multi_field_accuracy(
            predicted_by_id,
            gold_by_id,
            ("source_object_type_id", "target_object_type_id"),
        ),
        "link_cardinality_accuracy": _field_accuracy(
            predicted_by_id, gold_by_id, "cardinality"
        ),
        "link_stable_id_match": _id_coverage(predicted_by_id, gold_by_id),
        "link_business_name_match": _field_accuracy(
            predicted_by_id, gold_by_id, "name"
        ),
        "link_inverse_name_match": _field_accuracy(
            predicted_by_id, gold_by_id, "target_role_name"
        ),
        "physical_link_consistency": consistency,
        "link_to_physical_join_consistency": consistency,
    }


def _dimension_time_accuracy(
    predicted: dict[str, Any], gold: dict[str, Any]
) -> float | None:
    common_time = [
        key
        for key in predicted.keys() & gold.keys()
        if "time" in key.lower()
        or "date" in key.lower()
        or "time" in gold[key].property_id.lower()
        or "date" in gold[key].property_id.lower()
    ]
    return _ratio(
        sum(predicted[key].property_id == gold[key].property_id for key in common_time),
        len(common_time),
    )


def _dimension_role_consistency(
    resources: DraftResources, dimensions: dict[str, DimensionDefinition]
) -> float | None:
    properties = _index(resources.properties)
    comparable = [
        item
        for item in dimensions.values()
        if item.property_id in properties
    ]
    return _ratio(
        sum(
            str(properties[item.property_id].semantic_role)
            in {"DIMENSION", "STATUS", "TIME"}
            for item in comparable
        ),
        len(comparable),
    )


def _candidate_evidence_coverage(
    candidates: list[ConstructionCandidate] | None,
    resource_type: str,
    *,
    source_prefix: str | None = None,
) -> float | None:
    if candidates is None:
        return None
    selected = [item for item in candidates if item.resource_type == resource_type]
    if source_prefix is None:
        covered = sum(bool(item.evidence) for item in selected)
    else:
        covered = sum(
            any(str(evidence.source_type).startswith(source_prefix) for evidence in item.evidence)
            for item in selected
        )
    return _ratio(covered, len(selected))


def _metric_filter_support_confidence(
    candidates: list[ConstructionCandidate] | None,
) -> float | None:
    if candidates is None:
        return None
    metric_candidates = [
        item for item in candidates if item.resource_type == "metric"
    ]
    supported = [
        item
        for item in metric_candidates
        if item.original_candidate.get("filter_support")
    ]
    if not metric_candidates:
        return None
    return len(supported) / len(metric_candidates)


def _original_resource(original: dict[str, Any]) -> dict[str, Any]:
    return next(
        (value for value in original.values() if isinstance(value, dict) and "id" in value),
        {},
    )


def _changed_field_names(
    original: dict[str, Any], current: dict[str, Any]
) -> set[str]:
    resource = _original_resource(original)
    return {
        key
        for key in resource.keys() | current.keys()
        if resource.get(key) != current.get(key)
        and key not in {"created_at", "updated_at", "lifecycle_status"}
    }


def _edit_category(field: str) -> str:
    if field in PHYSICAL_FIELDS:
        return "physical"
    if field in SEMANTIC_FIELDS:
        return "semantic"
    if field in STRUCTURAL_FIELDS:
        return "structural"
    return "structural"


def _manual_creation_cost(gold: DraftResources) -> dict[str, Any]:
    collections = {
        "object": gold.object_types,
        "property": gold.properties,
        "binding": gold.bindings,
        "link": gold.link_types,
        "physical_join": gold.physical_joins,
        "dimension": gold.dimensions,
        "metric": gold.metrics,
    }
    resource_counts = {key: len(value) for key, value in collections.items()}
    populated_fields = sum(
        sum(
            value not in (None, "", [], {})
            and field not in {"created_at", "updated_at", "lifecycle_status"}
            for field, value in item.model_dump(mode="json").items()
        )
        for items in collections.values()
        for item in items
    )
    resource_count = sum(resource_counts.values())
    return {
        "manual_creation_resource_counts": resource_counts,
        "manual_creation_field_fill_count": populated_fields,
        "manual_creation_operations": resource_count + populated_fields,
    }
