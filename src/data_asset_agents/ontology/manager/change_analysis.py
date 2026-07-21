"""Deterministic Draft diff and downstream impact analysis."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine, text

from data_asset_agents.ontology.models import OntologyBundle

from .governance_models import (
    BreakingLevel,
    OntologyChangeSet,
    OntologyImpactReport,
    ResourceChange,
)
from .models import LifecycleStatus, OntologyDraftAggregate
from .repository import OntologyManagerRepository


def _stable_payload(resource: Any) -> dict[str, Any]:
    payload = resource.model_dump(mode="json")
    for field in (
        "created_at",
        "updated_at",
        "last_inspected_at",
        "schema_hash",
        "sync_status",
        "error_message",
    ):
        payload.pop(field, None)
    return payload


class OntologyChangeAnalyzer:
    def __init__(
        self,
        repository: OntologyManagerRepository,
        bundle: OntologyBundle,
        engine: Engine | None = None,
    ) -> None:
        self.repository = repository
        self.bundle = bundle
        self.engine = engine

    @staticmethod
    def _compare(
        resource_type: str,
        before_items: Iterable[Any],
        after_items: Iterable[Any],
    ) -> list[ResourceChange]:
        before = {item.id: item for item in before_items}
        after = {item.id: item for item in after_items}
        changes: list[ResourceChange] = []
        for resource_id in sorted(before.keys() | after.keys()):
            old, new = before.get(resource_id), after.get(resource_id)
            if old is None:
                changes.append(
                    ResourceChange(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        change_type="ADDED",
                        after=_stable_payload(new),
                        breaking_level=BreakingLevel.NON_BREAKING,
                        reason=f"Added {resource_type}",
                    )
                )
                continue
            if new is None:
                changes.append(
                    ResourceChange(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        change_type="REMOVED",
                        before=_stable_payload(old),
                        breaking_level=BreakingLevel.BREAKING,
                        reason=f"Removed published {resource_type}",
                    )
                )
                continue
            old_payload, new_payload = _stable_payload(old), _stable_payload(new)
            if old_payload == new_payload:
                continue
            deprecated = (
                getattr(old, "lifecycle_status", None) == LifecycleStatus.ACTIVE
                and getattr(new, "lifecycle_status", None) == LifecycleStatus.DEPRECATED
            )
            breaking = deprecated
            reason = f"Modified {resource_type}"
            if resource_type == "object_type" and (
                old.primary_key_property_id != new.primary_key_property_id
            ):
                breaking, reason = True, "Changed object primary key"
            elif resource_type == "property" and old.data_type != new.data_type:
                breaking, reason = True, "Changed property data type"
            elif resource_type in {"binding", "physical_join"}:
                breaking, reason = True, f"Changed physical {resource_type} contract"
            elif resource_type == "dimension" and old.property_id != new.property_id:
                breaking, reason = True, "Changed Dimension Property reference"
            elif resource_type == "metric":
                semantic_fields = {
                    "measure_property_id",
                    "aggregation",
                    "filter_predicates",
                    "time_property_id",
                    "supported_dimension_ids",
                }
                if any(
                    old_payload.get(field) != new_payload.get(field)
                    for field in semantic_fields
                ):
                    breaking, reason = True, "Changed Metric calculation contract"
            non_breaking_fields = {"name", "description", "synonyms"}
            changed_fields = {
                key
                for key in old_payload.keys() | new_payload.keys()
                if old_payload.get(key) != new_payload.get(key)
            }
            presentation_only = changed_fields <= non_breaking_fields
            changes.append(
                ResourceChange(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    change_type="DEPRECATED" if deprecated else "MODIFIED",
                    before=old_payload,
                    after=new_payload,
                    breaking_level=(
                        BreakingLevel.BREAKING
                        if breaking
                        else BreakingLevel.NON_BREAKING
                        if presentation_only
                        else BreakingLevel.POTENTIALLY_BREAKING
                    ),
                    reason=reason,
                )
            )
        return changes

    def diff(self, aggregate: OntologyDraftAggregate) -> OntologyChangeSet:
        _, base = self.repository.published_resources(aggregate.draft.base_version_id)
        current = aggregate.resources
        objects = self._compare("object_type", base.object_types, current.object_types)
        properties = self._compare("property", base.properties, current.properties)
        links = self._compare("link_type", base.link_types, current.link_types)
        bindings = self._compare("binding", base.bindings, current.bindings)
        joins = self._compare(
            "physical_join", base.physical_joins, current.physical_joins
        )
        metrics = self._compare("metric", base.metrics, current.metrics)
        dimensions = self._compare("dimension", base.dimensions, current.dimensions)
        result = OntologyChangeSet(
            draft_id=aggregate.draft.id,
            base_version_id=aggregate.draft.base_version_id,
            added_objects=[x for x in objects if x.change_type == "ADDED"],
            modified_objects=[x for x in objects if x.change_type == "MODIFIED"],
            deprecated_objects=[x for x in objects if x.change_type == "DEPRECATED"],
            removed_objects=[x for x in objects if x.change_type == "REMOVED"],
            added_properties=[x for x in properties if x.change_type == "ADDED"],
            modified_properties=[x for x in properties if x.change_type == "MODIFIED"],
            removed_properties=[x for x in properties if x.change_type == "REMOVED"],
            added_links=[x for x in links if x.change_type == "ADDED"],
            modified_links=[x for x in links if x.change_type == "MODIFIED"],
            removed_links=[x for x in links if x.change_type == "REMOVED"],
            changed_bindings=bindings,
            changed_physical_joins=joins,
            added_metrics=[x for x in metrics if x.change_type == "ADDED"],
            modified_metrics=[x for x in metrics if x.change_type == "MODIFIED"],
            deprecated_metrics=[x for x in metrics if x.change_type == "DEPRECATED"],
            removed_metrics=[x for x in metrics if x.change_type == "REMOVED"],
            added_dimensions=[x for x in dimensions if x.change_type == "ADDED"],
            modified_dimensions=[x for x in dimensions if x.change_type == "MODIFIED"],
            deprecated_dimensions=[x for x in dimensions if x.change_type == "DEPRECATED"],
            removed_dimensions=[x for x in dimensions if x.change_type == "REMOVED"],
        )
        self._persist("ontology_draft_change_set", aggregate.draft.id, result)
        return result

    def impact(
        self, aggregate: OntologyDraftAggregate, changes: OntologyChangeSet
    ) -> OntologyImpactReport:
        changed_properties = {
            item.resource_id
            for item in changes.changes
            if item.resource_type == "property"
        }
        changed_links = {
            item.resource_id
            for item in changes.changes
            if item.resource_type in {"link_type", "physical_join"}
        }
        changed_metric_ids = {
            item.resource_id
            for item in changes.changes
            if item.resource_type == "metric"
        }
        changed_dimension_ids = {
            item.resource_id
            for item in changes.changes
            if item.resource_type == "dimension"
        }
        affected_metrics = sorted(
            {
                *changed_metric_ids,
                *(
                    metric.id
                    for metric in aggregate.resources.metrics
                    if changed_properties
                    & {
                        metric.measure_property_id,
                        metric.time_property_id,
                        *(item.property_id for item in metric.filter_predicates),
                    }
                    or changed_dimension_ids & set(metric.supported_dimension_ids)
                ),
            }
        )
        affected_dimensions = sorted(
            {
                *changed_dimension_ids,
                *(
                    item.id
                    for item in aggregate.resources.dimensions
                    if item.property_id in changed_properties
                ),
            }
        )
        affected_assets = self._affected_sql_assets(changes)
        breaking = [
            f"{item.resource_type}:{item.resource_id} — {item.reason}"
            for item in changes.changes
            if item.breaking_level == BreakingLevel.BREAKING
        ]
        physical_changed = any(
            item.resource_type in {"binding", "physical_join"}
            for item in changes.changes
        )
        analytical_changed = bool(changed_metric_ids or changed_dimension_ids)
        report = OntologyImpactReport(
            draft_id=aggregate.draft.id,
            affected_metrics=affected_metrics,
            affected_dimensions=affected_dimensions,
            affected_physical_mappings=sorted(changed_properties),
            affected_link_paths=sorted(changed_links),
            affected_sql_assets=affected_assets,
            affected_benchmark_cases=(
                ["core-branch-card", "core-branch-rank", "join-*", "complex-*"]
                if changed_links or physical_changed
                else []
            ),
            rebuild_concept_index=bool(changes.changes),
            rebuild_sql_assets=physical_changed or analytical_changed or bool(affected_metrics),
            rerun_gold_hashes=physical_changed or analytical_changed or bool(affected_metrics),
            automatic_publish_allowed=not breaking,
            breaking_changes=breaking,
        )
        self._persist("ontology_draft_impact", aggregate.draft.id, report)
        return report

    def _affected_sql_assets(self, changes: OntologyChangeSet) -> list[str]:
        if self.engine is None:
            return []
        tokens: set[str] = set()
        for item in changes.changes:
            for payload in (item.before or {}, item.after or {}):
                tokens.update(
                    str(value)
                    for key, value in payload.items()
                    if key in {"table_name", "left_table", "right_table"}
                )
                if "property_bindings" in payload:
                    tokens.update(payload["property_bindings"].values())
        if not tokens:
            return []
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT asset_id, payload FROM sql_asset")).mappings()
            return sorted(
                str(row["asset_id"])
                for row in rows
                if any(token in json.dumps(row["payload"]) for token in tokens)
            )

    def _persist(self, table: str, draft_id: str, model: Any) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table}(draft_id,calculated_at,payload) "
                    "VALUES (:draft_id,:calculated_at,CAST(:payload AS jsonb)) "
                    "ON CONFLICT(draft_id) DO UPDATE SET "
                    "calculated_at=EXCLUDED.calculated_at,payload=EXCLUDED.payload"
                ),
                {
                    "draft_id": draft_id,
                    "calculated_at": model.calculated_at,
                    "payload": model.model_dump_json(),
                },
            )
