"""Emit fictional pre-DDL-009 state for the isolated upgrade acceptance test."""

from __future__ import annotations

import json
from pathlib import Path

from data_asset_agents.ontology.manager.models import DataSourceDefinition
from data_asset_agents.ontology.manager.seed_repository import ObjectOntologySeedRepository
from data_asset_agents.ontology.repository import YamlOntologyRepository

DRAFT_ID = "legacy-upgrade-draft"
VERSION_ID = "legacy-upgrade-version-id"


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return "'" + str(text).replace("'", "''") + "'"


def _json(value: object) -> str:
    return _literal(value) + "::jsonb"


def _resource_rows(
    table: str,
    id_column: str,
    resources: list[object],
    *,
    published: bool,
    resource_type: str,
) -> list[str]:
    owner_column = "ontology_version_id" if published else "draft_id"
    owner_id = VERSION_ID if published else DRAFT_ID
    rows: list[str] = []
    for resource in resources:
        payload = resource.model_dump(mode="json")  # type: ignore[attr-defined]
        columns = [owner_column, id_column, "payload"]
        values = [_literal(owner_id), _literal(payload["id"]), _json(payload)]
        if "binding" in table:
            columns.insert(2, "data_source_id")
            values.insert(2, _literal(payload["data_source_id"]))
        rows.append(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join(values)});"
        )
        if published:
            rows.append(
                "INSERT INTO ontology_version_object_resource("
                "ontology_version_id,resource_type,resource_id,source_draft_id) VALUES ("
                f"{_literal(VERSION_ID)},{_literal(resource_type)},"
                f"{_literal(payload['id'])},{_literal(DRAFT_ID)});"
            )
    return rows


def main() -> int:
    root = Path("ontology/retail_banking")
    bundle = YamlOntologyRepository(root).load()
    resources = ObjectOntologySeedRepository(
        {"retail_banking": root / "object_model"}
    ).load("retail_banking")
    data_source = DataSourceDefinition(
        id="minibank-postgres",
        name="MiniBank PostgreSQL",
        connection_ref="DATABASE_URL",
        description="Fictional upgrade acceptance data source",
    ).model_dump(mode="json")

    statements = [
        "BEGIN;",
        "INSERT INTO data_source_definition(data_source_id,provider,connection_ref,payload) "
        f"VALUES ({_literal(data_source['id'])},'POSTGRESQL','DATABASE_URL',"
        f"{_json(data_source)});",
        "INSERT INTO ontology_draft("
        "draft_id,name,description,status,created_by,created_at,updated_at,validation_report) "
        f"VALUES ({_literal(DRAFT_ID)},'Legacy fictional Draft',"
        "'Created only for isolated upgrade acceptance','VALIDATED','upgrade-acceptance',"
        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,"
        "'{\"valid\":true}'::jsonb);",
    ]
    draft_groups = [
        ("draft_object_type", "object_type_id", resources.object_types, "OBJECT_TYPE"),
        ("draft_property_definition", "property_id", resources.properties, "PROPERTY"),
        ("draft_link_type", "link_type_id", resources.link_types, "LINK_TYPE"),
        (
            "draft_object_data_source_binding",
            "binding_id",
            resources.bindings,
            "BINDING",
        ),
        (
            "draft_physical_join",
            "physical_join_id",
            resources.physical_joins,
            "PHYSICAL_JOIN",
        ),
        ("draft_metric_definition", "metric_id", resources.metrics, "METRIC"),
        (
            "draft_dimension_definition",
            "dimension_id",
            resources.dimensions,
            "DIMENSION",
        ),
    ]
    for table, id_column, items, resource_type in draft_groups:
        statements.extend(
            _resource_rows(
                table,
                id_column,
                items,
                published=False,
                resource_type=resource_type,
            )
        )

    bundle_payload = bundle.model_dump(mode="json")
    statements.append(
        "INSERT INTO ontology_version("
        "version_id,version,description,status,concept_count,mapping_count,join_count,"
        "published_by,is_current,bundle_json) VALUES ("
        f"{_literal(VERSION_ID)},'legacy-upgrade-v1',"
        "'Fictional pre-governance published version','PUBLISHED',"
        f"{len(bundle.concepts) + len(bundle.metrics) + len(bundle.dimensions)},"
        f"{len(bundle.mappings)},{len(bundle.joins)},'upgrade-acceptance',true,"
        f"{_json(bundle_payload)});"
    )
    published_groups = [
        ("published_object_type", "object_type_id", resources.object_types, "OBJECT_TYPE"),
        (
            "published_property_definition",
            "property_id",
            resources.properties,
            "PROPERTY",
        ),
        ("published_link_type", "link_type_id", resources.link_types, "LINK_TYPE"),
        (
            "published_object_data_source_binding",
            "binding_id",
            resources.bindings,
            "BINDING",
        ),
        (
            "published_physical_join",
            "physical_join_id",
            resources.physical_joins,
            "PHYSICAL_JOIN",
        ),
        (
            "published_metric_definition",
            "metric_id",
            resources.metrics,
            "METRIC",
        ),
        (
            "published_dimension_definition",
            "dimension_id",
            resources.dimensions,
            "DIMENSION",
        ),
    ]
    for table, id_column, items, resource_type in published_groups:
        statements.extend(
            _resource_rows(
                table,
                id_column,
                items,
                published=True,
                resource_type=resource_type,
            )
        )

    statements.extend(
        [
            "INSERT INTO sql_asset_build("
            "build_id,ontology_version_id,source_hash,source_path,status,completed_at) VALUES ("
            "'legacy-upgrade-sql-build','legacy-upgrade-version-id',repeat('a',64),"
            "'data/historical_sql/examples.json','READY',CURRENT_TIMESTAMP);",
            "INSERT INTO ontology_index_build("
            "build_id,ontology_version_id,index_type,status,source_hash,embedding_model,"
            "embedding_dimensions,document_count,started_at,completed_at,is_current) VALUES ("
            "'legacy-upgrade-index-build','legacy-upgrade-version-id','BUSINESS_CONCEPT',"
            "'READY',repeat('b',64),'fictional-embedding',1024,0,CURRENT_TIMESTAMP,"
            "CURRENT_TIMESTAMP,true);",
            "COMMIT;",
        ]
    )
    print("\n".join(statements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
