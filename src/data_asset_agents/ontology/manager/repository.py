"""Persistence for isolated drafts and atomically published object resources."""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Protocol

from sqlalchemy import Engine, text

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import OntologyBundle, OntologyVersion
from data_asset_agents.ontology.repository.postgres_repository import PostgresOntologyRepository

from .models import (
    DataSourceDefinition,
    DimensionDefinition,
    DraftResources,
    LinkType,
    MetricDefinition,
    ObjectDataSourceBinding,
    ObjectType,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PropertyDefinition,
)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)


class OntologyManagerRepository(Protocol):
    def save_data_source(self, source: DataSourceDefinition) -> None: ...
    def list_data_sources(self) -> list[DataSourceDefinition]: ...
    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate: ...
    def list_drafts(self) -> list[OntologyDraft]: ...
    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None: ...
    def save_draft(self, draft: OntologyDraft) -> None: ...
    def delete_draft(self, draft_id: str) -> None: ...
    def save_resource(self, draft_id: str, resource: Any) -> None: ...
    def delete_resource(self, draft_id: str, kind: str, resource_id: str) -> None: ...
    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]: ...
    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
    ) -> None: ...


RESOURCE_TABLES = {
    ObjectType: ("draft_object_type", "object_type_id"),
    PropertyDefinition: ("draft_property_definition", "property_id"),
    LinkType: ("draft_link_type", "link_type_id"),
    ObjectDataSourceBinding: ("draft_object_data_source_binding", "binding_id"),
    PhysicalJoinDefinition: ("draft_physical_join", "physical_join_id"),
    MetricDefinition: ("draft_metric_definition", "metric_id"),
    DimensionDefinition: ("draft_dimension_definition", "dimension_id"),
}
KIND_TABLES = {
    "object_type": ("draft_object_type", "object_type_id"),
    "property": ("draft_property_definition", "property_id"),
    "link_type": ("draft_link_type", "link_type_id"),
    "binding": ("draft_object_data_source_binding", "binding_id"),
    "physical_join": ("draft_physical_join", "physical_join_id"),
    "metric": ("draft_metric_definition", "metric_id"),
    "dimension": ("draft_dimension_definition", "dimension_id"),
}


class MemoryOntologyManagerRepository:
    """Small deterministic repository for state-machine and isolation tests."""

    def __init__(self) -> None:
        self.drafts: dict[str, OntologyDraftAggregate] = {}
        self.sources: dict[str, DataSourceDefinition] = {}
        self.current: tuple[str | None, DraftResources] = (None, DraftResources())
        self.versions: dict[str, DraftResources] = {}

    def save_data_source(self, source: DataSourceDefinition) -> None:
        self.sources[source.id] = deepcopy(source)

    def list_data_sources(self) -> list[DataSourceDefinition]:
        return list(deepcopy(self.sources).values())

    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate:
        if draft.id in self.drafts:
            raise OntologyError(f"Draft already exists: {draft.id}")
        aggregate = OntologyDraftAggregate(draft=draft, resources=resources or DraftResources())
        self.drafts[draft.id] = deepcopy(aggregate)
        return deepcopy(aggregate)

    def list_drafts(self) -> list[OntologyDraft]:
        return [deepcopy(item.draft) for item in self.drafts.values()]

    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None:
        item = self.drafts.get(draft_id)
        return deepcopy(item) if item else None

    def save_draft(self, draft: OntologyDraft) -> None:
        self.drafts[draft.id].draft = deepcopy(draft)

    def delete_draft(self, draft_id: str) -> None:
        self.drafts.pop(draft_id, None)

    def save_resource(self, draft_id: str, resource: Any) -> None:
        resources = self.drafts[draft_id].resources
        collection = {
            ObjectType: resources.object_types,
            PropertyDefinition: resources.properties,
            LinkType: resources.link_types,
            ObjectDataSourceBinding: resources.bindings,
            PhysicalJoinDefinition: resources.physical_joins,
            MetricDefinition: resources.metrics,
            DimensionDefinition: resources.dimensions,
        }[type(resource)]
        collection[:] = [item for item in collection if item.id != resource.id]
        collection.append(deepcopy(resource))

    def delete_resource(self, draft_id: str, kind: str, resource_id: str) -> None:
        resources = self.drafts[draft_id].resources
        collection = {
            "object_type": resources.object_types,
            "property": resources.properties,
            "link_type": resources.link_types,
            "binding": resources.bindings,
            "physical_join": resources.physical_joins,
            "metric": resources.metrics,
            "dimension": resources.dimensions,
        }[kind]
        collection[:] = [item for item in collection if item.id != resource_id]

    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]:
        if version_id is not None:
            return version_id, deepcopy(self.versions.get(version_id, DraftResources()))
        return deepcopy(self.current)

    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
    ) -> None:
        self.current = (version.id, deepcopy(resources))
        self.versions[version.id] = deepcopy(resources)
        self.drafts[draft.id] = OntologyDraftAggregate(
            draft=deepcopy(draft), resources=deepcopy(resources)
        )


class PostgresOntologyManagerRepository:
    """PostgreSQL implementation with one publication transaction."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save_data_source(self, source: DataSourceDefinition) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_source_definition(
                  data_source_id, provider, connection_ref, payload
                )
                VALUES (:id, :provider, :connection_ref, CAST(:payload AS jsonb))
                ON CONFLICT (data_source_id) DO UPDATE SET
                  provider=EXCLUDED.provider, connection_ref=EXCLUDED.connection_ref,
                  payload=EXCLUDED.payload
            """),
                {
                    "id": source.id,
                    "provider": source.provider,
                    "connection_ref": source.connection_ref,
                    "payload": _json(source),
                },
            )

    def list_data_sources(self) -> list[DataSourceDefinition]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT payload FROM data_source_definition ORDER BY data_source_id")
            ).scalars()
            return [DataSourceDefinition.model_validate(row) for row in rows]

    def create_draft(
        self, draft: OntologyDraft, resources: DraftResources | None = None
    ) -> OntologyDraftAggregate:
        with self.engine.begin() as connection:
            self._insert_draft(connection, draft)
            self._insert_resources(connection, draft.id, resources or DraftResources())
        return self.get_draft(draft.id) or OntologyDraftAggregate(draft=draft)

    @staticmethod
    def _insert_draft(connection: Any, draft: OntologyDraft) -> None:
        values = draft.model_dump(mode="python")
        values["validation_report"] = (
            _json(draft.validation_report) if draft.validation_report else None
        )
        connection.execute(
            text("""
            INSERT INTO ontology_draft
              (draft_id,name,description,base_version_id,source_snapshot_id,status,created_by,
               submitted_by,reviewed_by,created_at,updated_at,submitted_at,reviewed_at,
               validation_report,rejection_reason)
            VALUES (:id,:name,:description,:base_version_id,:source_snapshot_id,:status,:created_by,
               :submitted_by,:reviewed_by,:created_at,:updated_at,:submitted_at,:reviewed_at,
               CAST(:validation_report AS jsonb),:rejection_reason)
        """),
            values,
        )

    @staticmethod
    def _insert_resources(connection: Any, draft_id: str, resources: DraftResources) -> None:
        groups: list[Iterable[Any]] = [
            resources.object_types,
            resources.properties,
            resources.link_types,
            resources.bindings,
            resources.physical_joins,
            resources.metrics,
            resources.dimensions,
        ]
        for group in groups:
            for resource in group:
                table, key = RESOURCE_TABLES[type(resource)]
                values = {"draft_id": draft_id, "key": resource.id, "payload": _json(resource)}
                if isinstance(resource, ObjectDataSourceBinding):
                    connection.execute(
                        text(
                            f"INSERT INTO {table} "
                            f"(draft_id,{key},data_source_id,payload) "
                            "VALUES (:draft_id,:key,:data_source_id,"
                            "CAST(:payload AS jsonb))"
                        ),
                        {**values, "data_source_id": resource.data_source_id},
                    )
                else:
                    connection.execute(
                        text(
                            f"INSERT INTO {table} (draft_id,{key},payload) "
                            "VALUES (:draft_id,:key,CAST(:payload AS jsonb))"
                        ),
                        values,
                    )

    def list_drafts(self) -> list[OntologyDraft]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT * FROM ontology_draft ORDER BY updated_at DESC")
            ).mappings()
            return [self._draft(row) for row in rows]

    def get_draft(self, draft_id: str) -> OntologyDraftAggregate | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM ontology_draft WHERE draft_id=:id"), {"id": draft_id}
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return OntologyDraftAggregate(
                draft=self._draft(row),
                resources=DraftResources(
                    object_types=self._payloads(
                        connection, "draft_object_type", draft_id, ObjectType
                    ),
                    properties=self._payloads(
                        connection, "draft_property_definition", draft_id, PropertyDefinition
                    ),
                    link_types=self._payloads(connection, "draft_link_type", draft_id, LinkType),
                    bindings=self._payloads(
                        connection,
                        "draft_object_data_source_binding",
                        draft_id,
                        ObjectDataSourceBinding,
                    ),
                    physical_joins=self._payloads(
                        connection,
                        "draft_physical_join",
                        draft_id,
                        PhysicalJoinDefinition,
                    ),
                    metrics=self._payloads(
                        connection, "draft_metric_definition", draft_id, MetricDefinition
                    ),
                    dimensions=self._payloads(
                        connection,
                        "draft_dimension_definition",
                        draft_id,
                        DimensionDefinition,
                    ),
                ),
            )

    @staticmethod
    def _payloads(connection: Any, table: str, draft_id: str, model: Any) -> list[Any]:
        rows = connection.execute(
            text(f"SELECT payload FROM {table} WHERE draft_id=:id ORDER BY 1"), {"id": draft_id}
        ).scalars()
        return [model.model_validate(row) for row in rows]

    @staticmethod
    def _draft(row: Any) -> OntologyDraft:
        return OntologyDraft(
            id=row["draft_id"],
            name=row["name"],
            description=row["description"],
            base_version_id=row["base_version_id"],
            source_snapshot_id=row["source_snapshot_id"],
            status=row["status"],
            created_by=row["created_by"],
            submitted_by=row["submitted_by"],
            reviewed_by=row["reviewed_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            submitted_at=row["submitted_at"],
            reviewed_at=row["reviewed_at"],
            validation_report=row["validation_report"],
            rejection_reason=row["rejection_reason"],
        )

    def save_draft(self, draft: OntologyDraft) -> None:
        values = draft.model_dump(mode="python")
        values["validation_report"] = (
            _json(draft.validation_report) if draft.validation_report else None
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE ontology_draft SET name=:name,description=:description,status=:status,
                  submitted_by=:submitted_by,reviewed_by=:reviewed_by,updated_at=:updated_at,
                  submitted_at=:submitted_at,reviewed_at=:reviewed_at,
                  validation_report=CAST(:validation_report AS jsonb),
                  rejection_reason=:rejection_reason
                WHERE draft_id=:id
            """),
                values,
            )

    def delete_draft(self, draft_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ontology_draft WHERE draft_id=:id"), {"id": draft_id}
            )

    def save_resource(self, draft_id: str, resource: Any) -> None:
        table, key = RESOURCE_TABLES[type(resource)]
        values = {"draft_id": draft_id, "key": resource.id, "payload": _json(resource)}
        with self.engine.begin() as connection:
            if isinstance(resource, ObjectDataSourceBinding):
                connection.execute(
                    text(
                        f"INSERT INTO {table}(draft_id,{key},data_source_id,payload) "
                        "VALUES (:draft_id,:key,:data_source_id,CAST(:payload AS jsonb)) "
                        f"ON CONFLICT(draft_id,{key}) DO UPDATE SET "
                        "data_source_id=EXCLUDED.data_source_id,payload=EXCLUDED.payload"
                    ),
                    {**values, "data_source_id": resource.data_source_id},
                )
            else:
                connection.execute(
                    text(
                        f"INSERT INTO {table}(draft_id,{key},payload) "
                        "VALUES (:draft_id,:key,CAST(:payload AS jsonb)) "
                        f"ON CONFLICT(draft_id,{key}) DO UPDATE SET payload=EXCLUDED.payload"
                    ),
                    values,
                )

    def delete_resource(self, draft_id: str, kind: str, resource_id: str) -> None:
        table, key = KIND_TABLES[kind]
        with self.engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM {table} WHERE draft_id=:draft_id AND {key}=:id"),
                {"draft_id": draft_id, "id": resource_id},
            )

    def published_resources(
        self, version_id: str | None = None
    ) -> tuple[str | None, DraftResources]:
        with self.engine.connect() as connection:
            target = (
                version_id
                or connection.execute(
                    text(
                        "SELECT version_id FROM ontology_version "
                        "WHERE is_current AND status='PUBLISHED' LIMIT 1"
                    )
                ).scalar_one_or_none()
            )
            if target is None:
                return None, DraftResources()

            def rows(table: str, model: Any) -> list[Any]:
                payloads = connection.execute(
                    text(f"SELECT payload FROM {table} WHERE ontology_version_id=:id"),
                    {"id": target},
                ).scalars()
                return [model.model_validate(item) for item in payloads]

            return target, DraftResources(
                object_types=rows("published_object_type", ObjectType),
                properties=rows("published_property_definition", PropertyDefinition),
                link_types=rows("published_link_type", LinkType),
                bindings=rows("published_object_data_source_binding", ObjectDataSourceBinding),
                physical_joins=rows("published_physical_join", PhysicalJoinDefinition),
                metrics=rows("published_metric_definition", MetricDefinition),
                dimensions=rows("published_dimension_definition", DimensionDefinition),
            )

    def publish(
        self,
        draft: OntologyDraft,
        resources: DraftResources,
        version: OntologyVersion,
        bundle: OntologyBundle,
    ) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE ontology_version SET is_current=false WHERE is_current")
                )
                connection.execute(
                    text("""
                    INSERT INTO ontology_version(
                      version_id,version,description,source_snapshot_id,status,
                      concept_count,mapping_count,join_count,published_at,published_by,
                      is_current,bundle_json
                    ) VALUES (
                      :id,:version,:description,:snapshot,'PUBLISHED',
                      :concept_count,:mapping_count,
                      :join_count,:published_at,:published_by,true,CAST(:bundle AS jsonb))
                """),
                    {
                        "id": version.id,
                        "version": version.version,
                        "description": version.description,
                        "snapshot": version.source_snapshot_id,
                        "concept_count": version.concept_count,
                        "mapping_count": version.mapping_count,
                        "join_count": version.join_count,
                        "published_at": version.published_at,
                        "published_by": version.published_by,
                        "bundle": _json(bundle),
                    },
                )
                PostgresOntologyRepository._publish_rows(connection, version.id, bundle, [])
                groups = [
                    (
                        "published_object_type",
                        "object_type_id",
                        "OBJECT_TYPE",
                        resources.object_types,
                    ),
                    (
                        "published_property_definition",
                        "property_id",
                        "PROPERTY",
                        resources.properties,
                    ),
                    ("published_link_type", "link_type_id", "LINK_TYPE", resources.link_types),
                    (
                        "published_object_data_source_binding",
                        "binding_id",
                        "BINDING",
                        resources.bindings,
                    ),
                    (
                        "published_physical_join",
                        "physical_join_id",
                        "PHYSICAL_JOIN",
                        resources.physical_joins,
                    ),
                    (
                        "published_metric_definition",
                        "metric_id",
                        "METRIC",
                        resources.metrics,
                    ),
                    (
                        "published_dimension_definition",
                        "dimension_id",
                        "DIMENSION",
                        resources.dimensions,
                    ),
                ]
                for table, key, kind, items in groups:
                    for item in items:
                        fields = "ontology_version_id," + key + ",payload"
                        values = ":version_id,:key,CAST(:payload AS jsonb)"
                        params = {"version_id": version.id, "key": item.id, "payload": _json(item)}
                        if isinstance(item, ObjectDataSourceBinding):
                            fields = "ontology_version_id,binding_id,data_source_id,payload"
                            values = ":version_id,:key,:data_source_id,CAST(:payload AS jsonb)"
                            params["data_source_id"] = item.data_source_id
                        connection.execute(
                            text(f"INSERT INTO {table}({fields}) VALUES ({values})"), params
                        )
                        connection.execute(
                            text(
                                "INSERT INTO ontology_version_object_resource("
                                "ontology_version_id,resource_type,resource_id,source_draft_id"
                                ") VALUES (:version_id,:kind,:key,:draft_id)"
                            ),
                            {
                                "version_id": version.id,
                                "kind": kind,
                                "key": item.id,
                                "draft_id": draft.id,
                            },
                        )
                connection.execute(
                    text(
                        "UPDATE ontology_draft SET status='PUBLISHED',"
                        "reviewed_by=:reviewer,reviewed_at=:reviewed_at,"
                        "updated_at=:updated_at WHERE draft_id=:id"
                    ),
                    {
                        "id": draft.id,
                        "reviewer": draft.reviewed_by,
                        "reviewed_at": draft.reviewed_at,
                        "updated_at": draft.updated_at,
                    },
                )
        except Exception as exc:
            raise OntologyError(f"Atomic object ontology publication failed: {exc}") from exc
