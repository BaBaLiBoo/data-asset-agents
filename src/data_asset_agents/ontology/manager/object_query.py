"""Safe, read-only Object Explorer over the current published object model."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, text

from data_asset_agents.core.errors import OntologyError

from .governance_models import ObjectFilter, ObjectFilterOperator, ObjectRecord
from .models import DraftResources, ObjectDataSourceBinding, ObjectType, PropertyDefinition
from .repository import OntologyManagerRepository

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ObjectQueryService:
    """Build only allowlisted, parameterized SELECT statements from bindings."""

    OPERATORS = {
        ObjectFilterOperator.EQ: "=",
        ObjectFilterOperator.GT: ">",
        ObjectFilterOperator.GTE: ">=",
        ObjectFilterOperator.LT: "<",
        ObjectFilterOperator.LTE: "<=",
    }

    def __init__(self, engine: Engine, repository: OntologyManagerRepository) -> None:
        self.engine = engine
        self.repository = repository

    def _resources(
        self,
    ) -> tuple[str, DraftResources, dict[str, ObjectType], dict[str, PropertyDefinition]]:
        version_id, resources = self.repository.published_resources()
        if version_id is None:
            raise OntologyError("No published object ontology is active")
        return (
            version_id,
            resources,
            {item.id: item for item in resources.object_types},
            {item.id: item for item in resources.properties},
        )

    @staticmethod
    def _binding(resources: DraftResources, object_type_id: str) -> ObjectDataSourceBinding:
        binding = next(
            (item for item in resources.bindings if item.object_type_id == object_type_id),
            None,
        )
        if binding is None:
            raise OntologyError(f"Published object binding not found: {object_type_id}")
        return binding

    @staticmethod
    def _safe(value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise OntologyError(f"Unsafe published physical identifier: {value}")
        return value

    def list_objects(
        self,
        object_type_id: str,
        filters: list[ObjectFilter] | None = None,
        limit: int = 50,
    ) -> list[ObjectRecord]:
        if not 1 <= limit <= 100:
            raise OntologyError("Object Explorer limit must be between 1 and 100")
        version_id, resources, objects, properties = self._resources()
        obj = objects.get(object_type_id)
        if obj is None:
            raise OntologyError(f"Published object type not found: {object_type_id}")
        binding = self._binding(resources, object_type_id)
        selections = [
            f"o.{self._safe(column)} AS p_{index}"
            for index, column in enumerate(binding.property_bindings.values())
        ]
        clauses: list[str] = []
        parameters: dict[str, Any] = {"limit": limit}
        for index, item in enumerate(filters or []):
            column = binding.property_bindings.get(item.property_id)
            prop = properties.get(item.property_id)
            if column is None or prop is None or not prop.filterable:
                raise OntologyError(f"Property is not filterable: {item.property_id}")
            parameter = f"filter_{index}"
            if item.operator == ObjectFilterOperator.IN:
                if not isinstance(item.value, list) or not item.value:
                    raise OntologyError("IN filter requires a non-empty value list")
                names = []
                for value_index, value in enumerate(item.value):
                    name = f"{parameter}_{value_index}"
                    parameters[name] = value
                    names.append(f":{name}")
                clauses.append(f"o.{self._safe(column)} IN ({', '.join(names)})")
            else:
                if isinstance(item.value, list):
                    raise OntologyError("Scalar filter cannot accept a list")
                parameters[parameter] = item.value
                clauses.append(
                    f"o.{self._safe(column)} {self.OPERATORS[item.operator]} :{parameter}"
                )
        sql = (
            f"SELECT {', '.join(selections)} FROM "
            f"{self._safe(binding.schema_name)}.{self._safe(binding.table_name)} o"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY o.{self._safe(binding.primary_key_column)} LIMIT :limit"
        with self.engine.connect() as connection:
            rows = connection.execute(text(sql), parameters).mappings()
            return [
                self._record(
                    version_id, resources, obj, binding, properties, dict(row)
                )
                for row in rows
            ]

    def get_object(self, object_type_id: str, object_id: str) -> ObjectRecord | None:
        version_id, resources, objects, properties = self._resources()
        obj = objects.get(object_type_id)
        if obj is None:
            raise OntologyError(f"Published object type not found: {object_type_id}")
        binding = self._binding(resources, object_type_id)
        selections = [
            f"o.{self._safe(column)} AS p_{index}"
            for index, column in enumerate(binding.property_bindings.values())
        ]
        sql = (
            f"SELECT {', '.join(selections)} FROM "
            f"{self._safe(binding.schema_name)}.{self._safe(binding.table_name)} o "
            f"WHERE o.{self._safe(binding.primary_key_column)}=:object_id LIMIT 1"
        )
        with self.engine.connect() as connection:
            row = connection.execute(text(sql), {"object_id": object_id}).mappings().one_or_none()
        return (
            self._record(version_id, resources, obj, binding, properties, dict(row))
            if row is not None
            else None
        )

    def navigate(
        self,
        object_type_id: str,
        object_id: str,
        link_type_id: str,
        limit: int = 50,
    ) -> list[ObjectRecord]:
        if not 1 <= limit <= 100:
            raise OntologyError("Object Explorer limit must be between 1 and 100")
        version_id, resources, objects, properties = self._resources()
        link = next((item for item in resources.link_types if item.id == link_type_id), None)
        if link is None or object_type_id not in {
            link.source_object_type_id,
            link.target_object_type_id,
        }:
            raise OntologyError(f"Published link is unavailable: {link_type_id}")
        target_id = (
            link.target_object_type_id
            if object_type_id == link.source_object_type_id
            else link.source_object_type_id
        )
        source_binding = self._binding(resources, object_type_id)
        target_binding = self._binding(resources, target_id)
        join = next(
            (
                item
                for item in resources.physical_joins
                if item.id in link.physical_join_ids and item.enabled
            ),
            None,
        )
        if join is None:
            raise OntologyError(f"Link has no published physical join: {link_type_id}")
        if join.left_table == source_binding.table_name:
            source_column, target_column = join.left_column, join.right_column
        elif join.right_table == source_binding.table_name:
            source_column, target_column = join.right_column, join.left_column
        else:
            raise OntologyError("Published Link join does not match its object bindings")
        target = objects[target_id]
        selections = [
            f"t.{self._safe(column)} AS p_{index}"
            for index, column in enumerate(target_binding.property_bindings.values())
        ]
        sql = (
            f"SELECT {', '.join(selections)} FROM "
            f"{self._safe(source_binding.schema_name)}."
            f"{self._safe(source_binding.table_name)} s JOIN "
            f"{self._safe(target_binding.schema_name)}."
            f"{self._safe(target_binding.table_name)} t ON "
            f"s.{self._safe(source_column)}=t.{self._safe(target_column)} "
            f"WHERE s.{self._safe(source_binding.primary_key_column)}=:object_id "
            f"ORDER BY t.{self._safe(target_binding.primary_key_column)} LIMIT :limit"
        )
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(sql), {"object_id": object_id, "limit": limit}
            ).mappings()
            return [
                self._record(
                    version_id,
                    resources,
                    target,
                    target_binding,
                    properties,
                    dict(row),
                )
                for row in rows
            ]

    @staticmethod
    def _record(
        version_id: str,
        resources: DraftResources,
        obj: ObjectType,
        binding: ObjectDataSourceBinding,
        properties: dict[str, PropertyDefinition],
        row: dict[str, Any],
    ) -> ObjectRecord:
        values: dict[str, Any] = {}
        primary_key: Any = None
        for index, property_id in enumerate(binding.property_bindings):
            prop = properties[property_id]
            value = row.get(f"p_{index}")
            values[prop.name] = "***MASKED***" if prop.sensitive and value is not None else value
            if property_id == obj.primary_key_property_id:
                primary_key = value
        links = sorted(
            item.id
            for item in resources.link_types
            if obj.id in {item.source_object_type_id, item.target_object_type_id}
        )
        return ObjectRecord(
            object_type_id=obj.id,
            primary_key=primary_key,
            properties=values,
            available_links=links,
            ontology_version_id=version_id,
        )
