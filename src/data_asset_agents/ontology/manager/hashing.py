"""Deterministic hashes for authored ontology resources and runtime bundles."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from data_asset_agents.ontology.models import OntologyBundle

from .models import DraftResources

NON_SEMANTIC_FIELDS = {
    "created_at",
    "updated_at",
    "last_inspected_at",
    "sync_status",
    "error_message",
    "latest_snapshot_id",
    "schema_hash",
    "schema_columns",
    "primary_key_columns",
    "validation_report",
}


def _semantic_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: _semantic_value(item)
            for key, item in sorted(value.items())
            if key not in NON_SEMANTIC_FIELDS
        }
    if isinstance(value, list):
        normalized = [_semantic_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    return value


def canonical_resource_payload(resources: DraftResources) -> dict[str, list[dict[str, Any]]]:
    """Return the complete authored Draft in a stable type/id order."""

    groups = {
        "binding": resources.bindings,
        "dimension": resources.dimensions,
        "link_type": resources.link_types,
        "metric": resources.metrics,
        "object_type": resources.object_types,
        "physical_join": resources.physical_joins,
        "property": resources.properties,
    }
    return {
        kind: [_semantic_value(item) for item in sorted(items, key=lambda item: item.id)]
        for kind, items in sorted(groups.items())
    }


def canonical_json(value: Any) -> str:
    return json.dumps(
        _semantic_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def calculate_draft_resource_hash(resources: DraftResources) -> str:
    payload = canonical_json(canonical_resource_payload(resources))
    return hashlib.sha256(payload.encode()).hexdigest()


def calculate_bundle_hash(bundle: OntologyBundle | dict[str, Any]) -> str:
    payload = bundle.model_dump(mode="json") if isinstance(bundle, BaseModel) else bundle
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def has_semantic_change(before: DraftResources, after: DraftResources) -> bool:
    return calculate_draft_resource_hash(before) != calculate_draft_resource_hash(after)
