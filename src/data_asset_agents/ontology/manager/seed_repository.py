"""Allowlisted loading for direct object ontology seeds."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from data_asset_agents.core.errors import OntologyError

from .models import (
    DraftResources,
    LinkType,
    ObjectDataSourceBinding,
    ObjectType,
    PhysicalJoinDefinition,
    PropertyDefinition,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
SEED_TIMESTAMP = datetime(2026, 7, 1, tzinfo=UTC)


class ObjectOntologySeedRepository:
    """Read strongly typed resources from configured seed directories only."""

    def __init__(self, seeds: dict[str, Path]) -> None:
        if not seeds:
            raise ValueError("At least one object ontology seed must be configured")
        self._seeds = {name: path.resolve() for name, path in seeds.items()}

    @staticmethod
    def _load(path: Path, model: type[ModelT], *, timestamped: bool = False) -> list[ModelT]:
        if not path.is_file():
            raise OntologyError(f"Object ontology seed file is missing: {path.name}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            raise OntologyError(f"Object ontology seed {path.name} must contain a list")
        result: list[ModelT] = []
        try:
            for item in raw:
                if not isinstance(item, dict):
                    raise OntologyError(f"Object ontology seed {path.name} has a non-object row")
                payload: dict[str, Any] = dict(item)
                if timestamped:
                    payload.setdefault("created_at", SEED_TIMESTAMP)
                    payload.setdefault("updated_at", SEED_TIMESTAMP)
                result.append(model.model_validate(payload))
        except ValidationError as exc:
            raise OntologyError(f"Invalid object ontology seed {path.name}: {exc}") from exc
        return result

    @staticmethod
    def _duplicates(values: list[str]) -> set[str]:
        return {value for value in values if values.count(value) > 1}

    def load(self, seed_name: str) -> DraftResources:
        """Load one configured seed without filesystem paths, LLMs, or publication side effects."""

        root = self._seeds.get(seed_name)
        if root is None:
            raise OntologyError(
                f"Unknown object ontology seed {seed_name!r}; allowed={sorted(self._seeds)}"
            )
        resources = DraftResources(
            object_types=self._load(root / "objects.yaml", ObjectType, timestamped=True),
            properties=self._load(
                root / "properties.yaml", PropertyDefinition, timestamped=True
            ),
            bindings=self._load(root / "bindings.yaml", ObjectDataSourceBinding),
            link_types=self._load(root / "links.yaml", LinkType, timestamped=True),
            physical_joins=self._load(
                root / "physical_joins.yaml", PhysicalJoinDefinition
            ),
        )
        self._validate(resources)
        return resources

    def _validate(self, resources: DraftResources) -> None:
        groups = {
            "Object": [item.id for item in resources.object_types],
            "Property": [item.id for item in resources.properties],
            "Binding": [item.id for item in resources.bindings],
            "Link": [item.id for item in resources.link_types],
            "Physical Join": [item.id for item in resources.physical_joins],
        }
        for kind, identifiers in groups.items():
            duplicates = self._duplicates(identifiers)
            if duplicates:
                raise OntologyError(f"Duplicate {kind} ID(s): {sorted(duplicates)}")

        objects = {item.id: item for item in resources.object_types}
        properties = {item.id: item for item in resources.properties}
        joins = {item.id: item for item in resources.physical_joins}
        for prop in resources.properties:
            if prop.object_type_id not in objects:
                raise OntologyError(
                    f"Property {prop.id} references missing Object {prop.object_type_id}"
                )
        for obj in resources.object_types:
            owned = {
                prop.id for prop in resources.properties if prop.object_type_id == obj.id
            }
            if set(obj.property_ids) != owned:
                raise OntologyError(
                    f"Object {obj.id} property_ids do not match PropertyDefinition resources"
                )
        binding_objects: set[str] = set()
        for binding in resources.bindings:
            if binding.object_type_id not in objects:
                raise OntologyError(
                    f"Binding {binding.id} references missing Object {binding.object_type_id}"
                )
            if binding.object_type_id in binding_objects:
                raise OntologyError(
                    f"Object {binding.object_type_id} has conflicting primary bindings"
                )
            binding_objects.add(binding.object_type_id)
            for property_id in binding.property_bindings:
                prop = properties.get(property_id)
                if prop is None or prop.object_type_id != binding.object_type_id:
                    raise OntologyError(
                        f"Binding {binding.id} references missing or foreign Property {property_id}"
                    )
        for link in resources.link_types:
            if (
                link.source_object_type_id not in objects
                or link.target_object_type_id not in objects
            ):
                raise OntologyError(f"Link {link.id} references a missing Object")
            missing = set(link.physical_join_ids) - joins.keys()
            if missing:
                raise OntologyError(
                    f"Link {link.id} references missing Physical Join(s): {sorted(missing)}"
                )
