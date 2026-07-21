"""Metadata-driven, review-only object ontology candidate generation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import (
    HistoricalSQLAnalysis,
    MetadataSnapshot,
    TableAsset,
    TableMetadata,
)

from .classifier import TableRoleClassifier
from .inference import (
    infer_object_id,
    infer_property_contract,
    infer_property_name,
    stable_resource_id,
)
from .models import (
    BindingSyncStatus,
    CandidateEvidence,
    CandidateLinkType,
    CandidateObjectBinding,
    CandidateObjectType,
    CandidatePhysicalJoin,
    CandidateProperty,
    Cardinality,
    DraftResources,
    LifecycleStatus,
    LinkType,
    ObjectCandidateLLMOutput,
    ObjectCandidateSet,
    ObjectDataSourceBinding,
    ObjectType,
    PhysicalJoinDefinition,
    PropertyDefinition,
    SemanticRole,
    TableRole,
)


class ObjectFirstCandidateGenerator:
    """Generate candidates from metadata, profiles and parsed SQL without legacy mappings."""

    def __init__(
        self,
        settings: Settings,
        table_assets: list[TableAsset],
        factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings
        self.assets = {item.name: item for item in table_assets}
        self.factory = factory or ModelFactory(settings)
        self.classifier = TableRoleClassifier()

    def generate(
        self,
        snapshot: MetadataSnapshot,
        historical_sql: list[HistoricalSQLAnalysis] | None = None,
        existing: DraftResources | None = None,
    ) -> ObjectCandidateSet:
        historical_sql = historical_sql or []
        existing = existing or DraftResources()
        existing_objects = {item.id: item for item in existing.object_types}
        existing_properties = {item.id: item for item in existing.properties}
        existing_bindings = {item.object_type_id: item for item in existing.bindings}
        existing_links = {item.id for item in existing.link_types}
        existing_joins = {item.id for item in existing.physical_joins}
        table_to_object = {
            binding.table_name: binding.object_type_id for binding in existing.bindings
        }

        result = ObjectCandidateSet(snapshot_id=snapshot.id)
        eligible_tables: list[tuple[TableMetadata, TableRole, list[str]]] = []
        for table in sorted(snapshot.tables, key=lambda item: item.table_name):
            asset = self.assets.get(table.table_name)
            if asset is None:
                result.excluded_tables[table.table_name] = "not present in governed table assets"
                continue
            role, evidence = self.classifier.classify(asset)
            if role not in {TableRole.CANONICAL_OBJECT, TableRole.EVENT}:
                result.excluded_tables[table.table_name] = role.value
                continue
            eligible_tables.append((table, role, evidence))
            object_id = table_to_object.get(table.table_name, infer_object_id(table.table_name))
            table_to_object[table.table_name] = object_id
            properties, column_bindings = self._properties(
                snapshot.id,
                table,
                object_id,
                historical_sql,
                existing_properties,
                existing_bindings.get(object_id),
                snapshot.captured_at,
            )
            result.properties.extend(properties)

            if object_id not in existing_objects:
                candidate_object = self._object_candidate(
                    snapshot.id,
                    table,
                    object_id,
                    role,
                    evidence,
                    column_bindings,
                    snapshot.captured_at,
                )
                result.object_types.append(candidate_object)
                binding = self._binding(snapshot.id, table, object_id, column_bindings)
                result.bindings.append(binding)
            elif properties:
                previous = existing_bindings.get(object_id)
                if previous is not None:
                    merged = previous.model_copy(
                        update={
                            "property_bindings": {
                                **previous.property_bindings,
                                **column_bindings,
                            },
                            "latest_snapshot_id": snapshot.id,
                        }
                    )
                    result.bindings.append(
                        CandidateObjectBinding(
                            candidate_id=f"candidate-binding-{snapshot.id}-{merged.id}",
                            binding=merged,
                            confidence=0.94,
                            evidence=[
                                CandidateEvidence(
                                    source="metadata",
                                    detail="adds missing property bindings only",
                                )
                            ],
                        )
                    )

        joins = self._join_candidates(
            snapshot.id,
            eligible_tables,
            historical_sql,
            table_to_object,
            existing_links,
            existing_joins,
            snapshot.captured_at,
        )
        result.physical_joins.extend(item[0] for item in joins)
        result.link_types.extend(item[1] for item in joins)
        return result

    def _properties(
        self,
        snapshot_id: str,
        table: TableMetadata,
        object_id: str,
        historical_sql: list[HistoricalSQLAnalysis],
        existing_properties: dict[str, PropertyDefinition],
        existing_binding: ObjectDataSourceBinding | None,
        captured_at: datetime,
    ) -> tuple[list[CandidateProperty], dict[str, str]]:
        reverse = {
            column: property_id
            for property_id, column in (
                existing_binding.property_bindings.items() if existing_binding else []
            )
        }
        profiles = {item.column_name: item for item in table.profiles}
        sql_usage = {
            (reference.table, reference.column)
            for analysis in historical_sql
            for reference in analysis.columns
        }
        candidates: list[CandidateProperty] = []
        bindings: dict[str, str] = {}
        primary = set(table.primary_key)
        for column in table.columns:
            property_id = reverse.get(column.name) or (
                f"{object_id}.{infer_property_name(column.name)}"
            )
            bindings[property_id] = column.name
            if property_id in existing_properties:
                continue
            role, data_type = infer_property_contract(
                column.name, column.data_type, primary_key=column.name in primary
            )
            evidence = [
                CandidateEvidence(
                    source="metadata",
                    detail=f"{table.table_name}.{column.name} type={column.data_type}",
                )
            ]
            if column.name in primary:
                evidence.append(CandidateEvidence(source="primary-key", detail=column.name))
            profile = profiles.get(column.name)
            if profile is not None:
                evidence.append(
                    CandidateEvidence(
                        source="field-profile",
                        detail=(
                            f"null_rate={profile.null_rate}; unique_rate={profile.unique_rate}; "
                            f"samples={profile.sample_values[:3]}"
                        ),
                    )
                )
            if (table.table_name, column.name) in sql_usage or (None, column.name) in sql_usage:
                evidence.append(
                    CandidateEvidence(source="historical-sql", detail="reviewed SQL column usage")
                )
            candidates.append(
                CandidateProperty(
                    candidate_id=f"candidate-property-{snapshot_id}-{property_id}",
                    property=PropertyDefinition(
                        id=property_id,
                        object_type_id=object_id,
                        name=column.comment or infer_property_name(column.name).replace("_", " "),
                        description=(
                            f"Review candidate derived from {table.table_name}.{column.name}"
                        ),
                        data_type=data_type,
                        semantic_role=role,
                        nullable=column.nullable,
                        groupable=role
                        in {SemanticRole.DIMENSION, SemanticRole.STATUS, SemanticRole.TIME},
                        lifecycle_status=LifecycleStatus.DRAFT,
                        created_at=captured_at,
                        updated_at=captured_at,
                    ),
                    confidence=0.96 if column.name in primary else 0.88,
                    evidence=evidence,
                )
            )
        return candidates, bindings

    def _object_candidate(
        self,
        snapshot_id: str,
        table: TableMetadata,
        object_id: str,
        role: TableRole,
        role_evidence: list[str],
        bindings: dict[str, str],
        captured_at: datetime,
    ) -> CandidateObjectType:
        name = object_id.replace("_", " ")
        description = f"Review candidate inferred from governed {role.value} metadata"
        confidence = 0.93 if role == TableRole.CANONICAL_OBJECT else 0.88
        evidence = [CandidateEvidence(source="table-role", detail=item) for item in role_evidence]
        evidence.extend(
            [
                CandidateEvidence(source="metadata", detail=f"primary_key={table.primary_key}"),
                CandidateEvidence(
                    source="metadata", detail=f"foreign_keys={len(table.foreign_keys)}"
                ),
            ]
        )
        if self.settings.llm_mode != "mock":
            output = self._live_output(object_id, table.model_dump(mode="json"))
            name = output.object_name
            description = output.boundary_description
            confidence = output.confidence
            evidence.extend(CandidateEvidence(source="llm", detail=x) for x in output.evidence)
        primary_column = table.primary_key[0] if table.primary_key else next(iter(bindings), "")
        primary_property = next(
            (property_id for property_id, column in bindings.items() if column == primary_column),
            None,
        )
        return CandidateObjectType(
            candidate_id=f"candidate-object-{snapshot_id}-{object_id}",
            object_type=ObjectType(
                id=object_id,
                name=name,
                plural_name=f"{name} collection",
                description=description,
                primary_key_property_id=primary_property,
                property_ids=sorted(bindings),
                lifecycle_status=LifecycleStatus.DRAFT,
                created_at=captured_at,
                updated_at=captured_at,
            ),
            table_role=role,
            confidence=confidence,
            evidence=evidence,
        )

    @staticmethod
    def _binding(
        snapshot_id: str,
        table: TableMetadata,
        object_id: str,
        property_bindings: dict[str, str],
    ) -> CandidateObjectBinding:
        primary_column = table.primary_key[0] if table.primary_key else ""
        schema_hash = hashlib.sha256(
            json.dumps(table.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        binding = ObjectDataSourceBinding(
            id=f"{object_id}_primary_binding",
            object_type_id=object_id,
            data_source_id="minibank-postgres",
            schema_name=table.schema_name,
            table_name=table.table_name,
            primary_key_column=primary_column,
            property_bindings=property_bindings,
            latest_snapshot_id=snapshot_id,
            schema_hash=schema_hash,
            sync_status=BindingSyncStatus.UNCHECKED,
            schema_columns={item.name: item.data_type for item in table.columns},
            primary_key_columns=table.primary_key,
        )
        return CandidateObjectBinding(
            candidate_id=f"candidate-binding-{snapshot_id}-{binding.id}",
            binding=binding,
            confidence=0.97 if table.primary_key else 0.65,
            evidence=[CandidateEvidence(source="metadata", detail=table.table_name)],
        )

    def _join_candidates(
        self,
        snapshot_id: str,
        eligible_tables: list[tuple[TableMetadata, TableRole, list[str]]],
        historical_sql: list[HistoricalSQLAnalysis],
        table_to_object: dict[str, str],
        existing_links: set[str],
        existing_joins: set[str],
        captured_at: datetime,
    ) -> list[tuple[CandidatePhysicalJoin, CandidateLinkType]]:
        evidence_rows: dict[tuple[str, str, str, str], list[CandidateEvidence]] = {}
        for table, _, _ in eligible_tables:
            for foreign_key in table.foreign_keys:
                for left, right in zip(
                    foreign_key.constrained_columns,
                    foreign_key.referred_columns,
                    strict=True,
                ):
                    key = (table.table_name, left, foreign_key.referred_table, right)
                    evidence_rows.setdefault(key, []).append(
                        CandidateEvidence(
                            source="foreign-key", detail=foreign_key.name or "database FK"
                        )
                    )
        for analysis in historical_sql:
            if not analysis.certified:
                continue
            for join in analysis.joins:
                key = (join.left_table, join.left_column, join.right_table, join.right_column)
                evidence_rows.setdefault(key, []).append(
                    CandidateEvidence(source="historical-sql", detail=analysis.id)
                )

        result: list[tuple[CandidatePhysicalJoin, CandidateLinkType]] = []
        for (left_table, left_column, right_table, right_column), evidence in sorted(
            evidence_rows.items()
        ):
            left_object = table_to_object.get(left_table)
            right_object = table_to_object.get(right_table)
            if not left_object or not right_object or left_object == right_object:
                continue
            join_id = stable_resource_id(left_object, right_object, "join")
            link_id = stable_resource_id(left_object, "to", right_object)
            if join_id in existing_joins or link_id in existing_links:
                continue
            physical_join = PhysicalJoinDefinition(
                id=join_id,
                name=f"{left_object} to {right_object} join",
                description="Review candidate derived from physical evidence",
                left_table=left_table,
                left_column=left_column,
                right_table=right_table,
                right_column=right_column,
                cardinality=Cardinality.MANY_TO_ONE,
                confidence=0.98 if any(x.source == "foreign-key" for x in evidence) else 0.82,
                evidence=[f"{item.source}: {item.detail}" for item in evidence],
                lifecycle_status=LifecycleStatus.DRAFT,
            )
            link = LinkType(
                id=link_id,
                name=f"{left_object} relates to {right_object}",
                description="Business Link candidate; physical evidence remains separate",
                source_object_type_id=left_object,
                target_object_type_id=right_object,
                source_role_name=right_object,
                target_role_name=f"{left_object}_items",
                cardinality=Cardinality.MANY_TO_ONE,
                physical_join_ids=[join_id],
                lifecycle_status=LifecycleStatus.DRAFT,
                created_at=captured_at,
                updated_at=captured_at,
            )
            result.append(
                (
                    CandidatePhysicalJoin(
                        candidate_id=f"candidate-physical-join-{snapshot_id}-{join_id}",
                        physical_join=physical_join,
                        confidence=physical_join.confidence,
                        evidence=evidence,
                    ),
                    CandidateLinkType(
                        candidate_id=f"candidate-link-{snapshot_id}-{link_id}",
                        link_type=link,
                        confidence=physical_join.confidence,
                        evidence=evidence,
                    ),
                )
            )
        return result

    def _live_output(self, object_id: str, table: dict[str, object]) -> ObjectCandidateLLMOutput:
        prompt = (
            "Generate review-only business names and descriptions from masked metadata. "
            "Never output physical bindings, joins, lifecycle, policies, SQL, "
            "or publication state.\n"
            + json.dumps({"object_hint": object_id, "metadata": table}, ensure_ascii=False)
        )
        model = self.factory.chat_model().with_structured_output(ObjectCandidateLLMOutput)
        return ObjectCandidateLLMOutput.model_validate(model.invoke(prompt))
