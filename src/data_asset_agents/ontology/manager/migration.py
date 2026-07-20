"""Idempotent migration from the reviewed field-level bundle to object resources."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from data_asset_agents.ontology.models import JoinDefinition, OntologyBundle

from .classifier import TableRoleClassifier
from .models import (
    BindingSyncStatus,
    Cardinality,
    DataSourceDefinition,
    DraftResources,
    LifecycleStatus,
    LinkType,
    ObjectDataSourceBinding,
    ObjectType,
    PhysicalJoinDefinition,
    PropertyDataType,
    PropertyDefinition,
    SemanticRole,
)

JOIN_IDS = {
    ("dwd_card_transaction", "dim_branch"): "transaction_branch_join",
    ("dwd_card_transaction", "dim_card"): "transaction_card_join",
    ("dwd_card_transaction", "dim_customer"): "transaction_customer_join",
    ("dwd_card_transaction", "dim_merchant"): "transaction_merchant_join",
    ("dim_card", "dim_account"): "card_account_join",
    ("dim_account", "dim_customer"): "account_customer_join",
    ("dwd_account_transaction", "dim_account"): "account_transaction_account_join",
}

LINK_SPECS = {
    frozenset(("customer", "account")): (
        "customer_owns_account",
        "客户持有账户",
        "customer",
        "accounts",
        Cardinality.ONE_TO_MANY,
    ),
    frozenset(("account", "card")): (
        "account_has_card",
        "账户关联银行卡",
        "account",
        "cards",
        Cardinality.ONE_TO_MANY,
    ),
    frozenset(("card", "transaction")): (
        "card_generates_transaction",
        "银行卡产生交易",
        "card",
        "transactions",
        Cardinality.ONE_TO_MANY,
    ),
    frozenset(("transaction", "branch")): (
        "transaction_belongs_to_branch",
        "交易归属分行",
        "branch",
        "transactions",
        Cardinality.MANY_TO_ONE,
    ),
    frozenset(("transaction", "merchant")): (
        "transaction_occurs_at_merchant",
        "交易发生于商户",
        "merchant",
        "transactions",
        Cardinality.MANY_TO_ONE,
    ),
}


def physical_join_id(join: JoinDefinition) -> str:
    direct = JOIN_IDS.get((join.left_table, join.right_table))
    reverse = JOIN_IDS.get((join.right_table, join.left_table))
    return direct or reverse or f"{join.left_table}_{join.right_table}_join"


def adapt_physical_joins(joins: Iterable[JoinDefinition]) -> list[PhysicalJoinDefinition]:
    return [
        PhysicalJoinDefinition(
            id=physical_join_id(join),
            name=physical_join_id(join).replace("_", " "),
            description="Migrated reviewed physical join",
            cardinality=Cardinality.MANY_TO_ONE,
            evidence=["reviewed joins.yaml compatibility seed"],
            **join.model_dump(),
        )
        for join in joins
    ]


def _role_and_type(role: str, column: str) -> tuple[SemanticRole, PropertyDataType]:
    lowered = role.lower()
    if lowered.endswith("_id") or column.lower().endswith("_id"):
        return SemanticRole.ATTRIBUTE, PropertyDataType.INTEGER
    if lowered in {"amount", "txn_amount_cny"} or "amount" in column.lower():
        return SemanticRole.MEASURE, PropertyDataType.DECIMAL
    if lowered == "status" or "status" in column.lower():
        return SemanticRole.STATUS, PropertyDataType.STRING
    if lowered in {"event_time", "date"} or any(x in column.lower() for x in ("date", "time")):
        return SemanticRole.TIME, PropertyDataType.DATE
    if lowered in {"channel", "type", "category", "name"} or any(
        x in column.lower() for x in ("type", "category", "channel", "name")
    ):
        return SemanticRole.DIMENSION, PropertyDataType.STRING
    return SemanticRole.ATTRIBUTE, PropertyDataType.STRING


class LegacyOntologyObjectMigrator:
    """Create stable draft resources from a reviewed legacy ontology bundle."""

    def __init__(self, bundle: OntologyBundle) -> None:
        self.bundle = bundle
        self.classifier = TableRoleClassifier()

    def migrate(self, source_snapshot_id: str | None = None) -> DraftResources:
        tables = {item.name: item for item in self.bundle.tables}
        object_mappings = {
            item.concept_id: item for item in self.bundle.mappings if ":" not in item.concept_id
        }
        concepts = {item.id: item for item in self.bundle.concepts if item.kind == "object"}
        properties: list[PropertyDefinition] = []
        objects: list[ObjectType] = []
        bindings: list[ObjectDataSourceBinding] = []
        table_to_object: dict[str, str] = {}

        for object_id in sorted(concepts):
            mapping = object_mappings.get(object_id)
            table = tables.get(mapping.table) if mapping else None
            if mapping is None or table is None or not self.classifier.eligible_for_object(table):
                continue
            role_bindings = dict(mapping.column_bindings)
            if object_id == "transaction":
                role_bindings.update(
                    {
                        "customer_id": "customer_id",
                        "card_id": "card_id",
                        "branch_id": "branch_id",
                        "merchant_id": "merchant_id",
                        "channel": "transaction_channel",
                        "card_type": "card_type",
                    }
                )
            primary_column = next(
                (column for column in role_bindings.values() if column == f"{object_id}_id"),
                next((column for column in role_bindings.values() if column.endswith("_id")), ""),
            )
            property_ids: list[str] = []
            property_bindings: dict[str, str] = {}
            for role, column in role_bindings.items():
                property_id = f"{object_id}.{role}"
                semantic_role, data_type = _role_and_type(role, column)
                if column == primary_column:
                    semantic_role = SemanticRole.IDENTIFIER
                properties.append(
                    PropertyDefinition(
                        id=property_id,
                        object_type_id=object_id,
                        name=role.replace("_", " "),
                        description=f"Reviewed {concepts[object_id].name} property",
                        data_type=data_type,
                        semantic_role=semantic_role,
                        nullable=column != primary_column,
                        groupable=semantic_role in {SemanticRole.DIMENSION, SemanticRole.STATUS},
                        sensitive=object_id == "transaction" and role == "customer_id",
                        unit="CNY" if semantic_role == SemanticRole.MEASURE else None,
                        lifecycle_status=LifecycleStatus.ACTIVE,
                    )
                )
                property_ids.append(property_id)
                property_bindings[property_id] = column
            # Preserve the requested business shape without inventing a nonexistent column.
            if object_id in {"account", "card"} and f"{object_id}.status" not in property_ids:
                property_id = f"{object_id}.status"
                properties.append(
                    PropertyDefinition(
                        id=property_id,
                        object_type_id=object_id,
                        name="status",
                        description="Business property awaiting a reviewed physical binding",
                        data_type=PropertyDataType.STRING,
                        semantic_role=SemanticRole.STATUS,
                        lifecycle_status=LifecycleStatus.ACTIVE,
                    )
                )
                property_ids.append(property_id)
            title_candidates = [
                f"{object_id}.name",
                f"{object_id}.customer_type",
                f"{object_id}.account_type",
                f"{object_id}.card_type",
            ]
            title = next((item for item in title_candidates if item in property_ids), None)
            objects.append(
                ObjectType(
                    id=object_id,
                    name=concepts[object_id].name,
                    plural_name=f"{concepts[object_id].name}集合",
                    description=concepts[object_id].description,
                    synonyms=concepts[object_id].synonyms,
                    primary_key_property_id=f"{object_id}.{primary_column}",
                    title_property_id=title,
                    property_ids=property_ids,
                    lifecycle_status=LifecycleStatus.ACTIVE,
                )
            )
            schema_hash = hashlib.sha256(
                f"public.{table.name}:{','.join(sorted(table.columns))}".encode()
            ).hexdigest()
            bindings.append(
                ObjectDataSourceBinding(
                    id=f"{object_id}_primary_binding",
                    object_type_id=object_id,
                    data_source_id="minibank-postgres",
                    table_name=table.name,
                    primary_key_column=primary_column,
                    property_bindings=property_bindings,
                    latest_snapshot_id=source_snapshot_id,
                    schema_hash=schema_hash,
                    sync_status=BindingSyncStatus.HEALTHY,
                )
            )
            table_to_object[table.name] = object_id

        physical_joins = adapt_physical_joins(self.bundle.joins)
        links: list[LinkType] = []
        for join in physical_joins:
            left_object = table_to_object.get(join.left_table)
            right_object = table_to_object.get(join.right_table)
            if not left_object or not right_object:
                continue
            spec = LINK_SPECS.get(frozenset((left_object, right_object)))
            if not spec:
                continue
            link_id, name, singular, plural, cardinality = spec
            source, target = (left_object, right_object)
            if link_id == "customer_owns_account":
                source, target = "customer", "account"
            elif link_id == "account_has_card":
                source, target = "account", "card"
            elif link_id == "card_generates_transaction":
                source, target = "card", "transaction"
            elif link_id.startswith("transaction_"):
                source, target = (
                    "transaction",
                    right_object if left_object == "transaction" else left_object,
                )
            links.append(
                LinkType(
                    id=link_id,
                    name=name,
                    source_object_type_id=source,
                    target_object_type_id=target,
                    source_role_name=singular,
                    target_role_name=plural,
                    cardinality=cardinality,
                    physical_join_ids=[join.id],
                    lifecycle_status=LifecycleStatus.ACTIVE,
                )
            )
        return DraftResources(
            object_types=objects,
            properties=sorted(properties, key=lambda item: item.id),
            link_types=sorted({item.id: item for item in links}.values(), key=lambda item: item.id),
            bindings=bindings,
            physical_joins=physical_joins,
        )

    @staticmethod
    def data_source() -> DataSourceDefinition:
        return DataSourceDefinition(
            id="minibank-postgres",
            name="MiniBank PostgreSQL",
            connection_ref="DATABASE_URL",
            description="Application-managed fictional MiniBank data source",
        )
