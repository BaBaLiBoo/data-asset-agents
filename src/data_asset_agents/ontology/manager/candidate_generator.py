"""Metadata-driven, review-only object ontology candidate generation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime

import sqlglot
from sqlglot import exp

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import (
    HistoricalSQLAnalysis,
    MetadataSnapshot,
    MetricAggregation,
    PropertyFilterPredicate,
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
    CandidateDimension,
    CandidateEvidence,
    CandidateLinkType,
    CandidateMetric,
    CandidateObjectBinding,
    CandidateObjectType,
    CandidatePhysicalJoin,
    CandidateProperty,
    Cardinality,
    CatalogMode,
    DimensionDefinition,
    DraftResources,
    EvidenceSourceType,
    LifecycleStatus,
    LinkType,
    MetricDefinition,
    MetricFilterSupport,
    ObjectCandidateLLMOutput,
    ObjectCandidateSet,
    ObjectDataSourceBinding,
    ObjectType,
    PhysicalJoinDefinition,
    PropertyDefinition,
    SemanticRole,
    TableRole,
)


def _certified_construction_evidence(analysis: HistoricalSQLAnalysis) -> bool:
    return analysis.certified and analysis.certification_level in {"GOLD", "CERTIFIED"}


def _dimension_id(prop: PropertyDefinition) -> str:
    suffix = prop.id.rsplit(".", 1)[-1]
    if suffix == "name":
        return prop.object_type_id
    if suffix.startswith(f"{prop.object_type_id}_"):
        return suffix
    if prop.semantic_role == SemanticRole.TIME:
        return stable_resource_id(prop.object_type_id, "date")
    return stable_resource_id(prop.object_type_id, suffix)


class ObjectFirstCandidateGenerator:
    """Generate candidates from metadata, profiles and parsed SQL without legacy mappings."""

    def __init__(
        self,
        settings: Settings,
        table_assets: list[TableAsset] | None = None,
        factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings
        self.assets = {item.name: item for item in (table_assets or [])}
        self.factory = factory or ModelFactory(settings)
        self.classifier = TableRoleClassifier()

    def generate(
        self,
        snapshot: MetadataSnapshot,
        historical_sql: list[HistoricalSQLAnalysis] | None = None,
        existing: DraftResources | None = None,
        *,
        catalog_mode: CatalogMode = CatalogMode.GOVERNED_CATALOG,
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
            if catalog_mode == CatalogMode.RAW_METADATA:
                role, evidence = self.classifier.classify_raw(table)
            else:
                asset = self.assets.get(table.table_name)
                if asset is None:
                    result.excluded_tables[table.table_name] = (
                        "not present in governed table assets"
                    )
                    continue
                role, evidence = self.classifier.classify(asset)
            if role not in {TableRole.CANONICAL_OBJECT, TableRole.EVENT}:
                result.excluded_tables[table.table_name] = role.value
                continue
            eligible_tables.append((table, role, evidence))
            object_id = table_to_object.get(table.table_name, infer_object_id(table.table_name))
            table_to_object[table.table_name] = object_id
            llm_output = (
                self._live_output(object_id, table.model_dump(mode="json"))
                if self.settings.llm_mode != "mock"
                else None
            )
            properties, column_bindings = self._properties(
                snapshot.id,
                table,
                object_id,
                historical_sql,
                existing_properties,
                existing_bindings.get(object_id),
                snapshot.captured_at,
            )
            if llm_output is not None:
                properties = self._apply_property_enrichment(
                    table, properties, llm_output
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
                    llm_output,
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
        result.dimensions.extend(
            self._dimension_candidates(snapshot.id, result.properties, historical_sql)
        )
        all_properties = [*existing.properties, *(item.property for item in result.properties)]
        all_dimensions = [*existing.dimensions, *(item.dimension for item in result.dimensions)]
        result.metrics.extend(
            self._metric_candidates(
                snapshot.id,
                historical_sql,
                table_to_object,
                all_properties,
                all_dimensions,
            )
        )
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
            if _certified_construction_evidence(analysis)
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
                        unit="CNY" if "amount_cny" in column.name.lower() else None,
                        synonyms=(
                            ["交易额"]
                            if "amount" in column.name.lower()
                            else ["渠道"]
                            if "channel" in column.name.lower()
                            else []
                        ),
                        lifecycle_status=LifecycleStatus.DRAFT,
                        created_at=captured_at,
                        updated_at=captured_at,
                    ),
                    confidence=0.96 if column.name in primary else 0.88,
                    evidence=evidence,
                    sensitive_suggestion=(
                        column.name not in primary
                        and column.name
                        in {
                            "customer_id",
                            "account_id",
                            "card_id",
                            "merchant_id",
                        }
                    ),
                    unit_suggestion=(
                        "CNY" if "amount_cny" in column.name.lower() else None
                    ),
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
        output: ObjectCandidateLLMOutput | None = None,
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
        if output is not None:
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
    def _apply_property_enrichment(
        table: TableMetadata,
        candidates: list[CandidateProperty],
        output: ObjectCandidateLLMOutput,
    ) -> list[CandidateProperty]:
        by_column = {
            candidate.property.id: (index, column.name)
            for index, (candidate, column) in enumerate(
                zip(candidates, table.columns, strict=False)
            )
        }
        enriched: list[CandidateProperty] = []
        for candidate in candidates:
            index, column_name = by_column.get(
                candidate.property.id, (-1, candidate.property.id.rsplit(".", 1)[-1])
            )
            updates: dict[str, object] = {}
            warnings = list(candidate.warnings)
            if 0 <= index < len(output.property_names) and output.property_names[index]:
                updates["name"] = output.property_names[index]
            suggested_role = output.property_roles.get(column_name)
            if suggested_role is not None:
                compatible = (
                    suggested_role == candidate.property.semantic_role
                    or suggested_role
                    in {
                        SemanticRole.ATTRIBUTE,
                        SemanticRole.STATUS,
                        SemanticRole.DIMENSION,
                    }
                    and candidate.property.data_type.value == "STRING"
                    or suggested_role == SemanticRole.TIME
                    and candidate.property.data_type.value in {"DATE", "DATETIME"}
                    or suggested_role == SemanticRole.MEASURE
                    and candidate.property.data_type.value in {"INTEGER", "DECIMAL"}
                )
                if compatible:
                    updates["semantic_role"] = suggested_role
                    updates["groupable"] = suggested_role in {
                        SemanticRole.DIMENSION,
                        SemanticRole.STATUS,
                        SemanticRole.TIME,
                    }
                else:
                    warnings.append(
                        f"LLM role {suggested_role} conflicts with deterministic type"
                    )
            evidence = [
                *candidate.evidence,
                CandidateEvidence(
                    source_type=EvidenceSourceType.LLM_SEMANTIC_SUGGESTION,
                    table_name=table.table_name,
                    column_name=column_name,
                    extracted_fact="display name/role suggestion",
                    confidence=output.confidence,
                    deterministic=False,
                    llm_generated=True,
                ),
            ]
            enriched.append(
                candidate.model_copy(
                    update={
                        "property": candidate.property.model_copy(update=updates),
                        "evidence": evidence,
                        "warnings": warnings,
                    }
                )
            )
        return enriched

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
            if not _certified_construction_evidence(analysis):
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

    @staticmethod
    def _dimension_candidates(
        snapshot_id: str,
        properties: list[CandidateProperty],
        historical_sql: list[HistoricalSQLAnalysis],
    ) -> list[CandidateDimension]:
        group_expressions = {
            expression.lower()
            for analysis in historical_sql
            if _certified_construction_evidence(analysis)
            for expression in analysis.group_by
        }
        candidates: list[CandidateDimension] = []
        for item in properties:
            prop = item.property
            if (
                not prop.groupable
                or prop.semantic_role
                not in {SemanticRole.DIMENSION, SemanticRole.STATUS, SemanticRole.TIME}
            ):
                continue
            physical = next(
                (
                    evidence
                    for evidence in item.evidence
                    if str(evidence.source_type) in {
                        EvidenceSourceType.COLUMN_METADATA,
                        EvidenceSourceType.TABLE_METADATA,
                    }
                    or evidence.source in {"metadata", "COLUMN_METADATA"}
                ),
                None,
            )
            column = prop.id.rsplit(".", 1)[-1]
            used_by_group = any(
                expression.endswith(f".{column}") or expression == column
                for expression in group_expressions
            )
            evidence = list(item.evidence)
            if used_by_group:
                evidence.append(
                    CandidateEvidence(
                        source_type=EvidenceSourceType.SQL_GROUP_BY,
                        source_snapshot_id=snapshot_id,
                        column_name=column,
                        extracted_fact="column occurs in certified SQL GROUP BY",
                    )
                )
            elif physical is None and prop.semantic_role != SemanticRole.TIME:
                continue
            dimension_id = _dimension_id(prop)
            candidates.append(
                CandidateDimension(
                    candidate_id=f"candidate-dimension-{snapshot_id}-{dimension_id}",
                    dimension=DimensionDefinition(
                        id=dimension_id,
                        name=prop.name,
                        description=f"Review candidate over {prop.id}",
                        property_id=prop.id,
                        synonyms=prop.synonyms,
                        lifecycle_status=LifecycleStatus.DRAFT,
                        created_at=prop.created_at,
                        updated_at=prop.updated_at,
                    ),
                    time_grain_suggestion=(
                        "DAY" if prop.semantic_role == SemanticRole.TIME else None
                    ),
                    confidence=0.94 if used_by_group else 0.78,
                    evidence=evidence,
                )
            )
        return candidates

    @staticmethod
    def _metric_candidates(
        snapshot_id: str,
        historical_sql: list[HistoricalSQLAnalysis],
        table_to_object: dict[str, str],
        properties: list[PropertyDefinition],
        dimensions: list[DimensionDefinition],
    ) -> list[CandidateMetric]:
        properties_by_id = {item.id: item for item in properties}
        dimensions_by_property = {item.property_id: item.id for item in dimensions}
        result: list[CandidateMetric] = []
        seen_metric_ids: set[str] = set()
        filter_occurrences: Counter[tuple[str, str, str]] = Counter()
        certified = [
            item for item in historical_sql if _certified_construction_evidence(item)
        ]
        parsed: list[tuple[HistoricalSQLAnalysis, exp.Expression, dict[str, str]]] = []
        for analysis in certified:
            try:
                statement = sqlglot.parse_one(analysis.sql_text, read="postgres")
            except sqlglot.errors.ParseError:
                continue
            aliases = {
                table.alias_or_name: table.name for table in statement.find_all(exp.Table)
            }
            parsed.append((analysis, statement, aliases))
            for equality in statement.find_all(exp.EQ):
                if isinstance(equality.left, exp.Column) and isinstance(
                    equality.right, exp.Literal
                ):
                    table = aliases.get(
                        equality.left.table, equality.left.table
                    ) or (
                        analysis.tables[0] if len(analysis.tables) == 1 else ""
                    )
                    if table:
                        filter_occurrences[
                            (table, equality.left.name, str(equality.right.this))
                        ] += 1

        for analysis, statement, aliases in parsed:
            physical_tables = list(analysis.tables)
            for aggregate in statement.find_all(exp.AggFunc):
                function = aggregate.key.upper()
                distinct = isinstance(aggregate.this, exp.Distinct)
                column = next(aggregate.find_all(exp.Column), None)
                if column is None:
                    continue
                table = aliases.get(column.table, column.table) or (
                    physical_tables[0] if len(physical_tables) == 1 else ""
                )
                object_id = table_to_object.get(table)
                if not object_id:
                    continue
                property_id = f"{object_id}.{infer_property_name(column.name)}"
                prop = properties_by_id.get(property_id)
                if prop is None:
                    continue
                aggregation_name = (
                    "COUNT_DISTINCT" if function == "COUNT" and distinct else function
                )
                try:
                    aggregation = MetricAggregation(aggregation_name)
                except ValueError:
                    continue
                alias = (
                    aggregate.parent.alias
                    if isinstance(aggregate.parent, exp.Alias)
                    else None
                )
                metric_id = (
                    stable_resource_id(alias)
                    if alias
                    else stable_resource_id(aggregation.value, prop.id)
                )
                if metric_id in seen_metric_ids:
                    continue
                seen_metric_ids.add(metric_id)
                predicates: list[PropertyFilterPredicate] = []
                supports: list[MetricFilterSupport] = []
                evidence = [
                    CandidateEvidence(
                        source_type=EvidenceSourceType.SQL_AGGREGATION,
                        source_snapshot_id=snapshot_id,
                        table_name=table,
                        column_name=column.name,
                        sql_asset_id=analysis.id,
                        extracted_fact=aggregate.sql(dialect="postgres"),
                    )
                ]
                for equality in statement.find_all(exp.EQ):
                    if not isinstance(equality.left, exp.Column) or not isinstance(
                        equality.right, exp.Literal
                    ):
                        continue
                    filter_table = aliases.get(
                        equality.left.table, equality.left.table
                    ) or table
                    if filter_table != table:
                        continue
                    filter_property_id = (
                        f"{object_id}.{infer_property_name(equality.left.name)}"
                    )
                    if filter_property_id not in properties_by_id:
                        continue
                    value = str(equality.right.this)
                    count = filter_occurrences[(table, equality.left.name, value)]
                    competing = sorted(
                        {
                            candidate_value
                            for candidate_table, candidate_column, candidate_value
                            in filter_occurrences
                            if candidate_table == table
                            and candidate_column == equality.left.name
                            and candidate_value != value
                        }
                    )
                    predicate = PropertyFilterPredicate(
                        property_id=filter_property_id, operator="EQ", value=value
                    )
                    predicates.append(predicate)
                    supports.append(
                        MetricFilterSupport(
                            predicate=predicate,
                            occurrence_count=count,
                            sql_coverage_count=count,
                            consistency_ratio=(
                                count
                                / max(
                                    1,
                                    sum(
                                        occurrences
                                        for (
                                            candidate_table,
                                            candidate_column,
                                            _,
                                        ), occurrences in filter_occurrences.items()
                                        if candidate_table == table
                                        and candidate_column == equality.left.name
                                    ),
                                )
                            ),
                            conflicting_values=competing,
                        )
                    )
                    evidence.append(
                        CandidateEvidence(
                            source_type=EvidenceSourceType.SQL_FILTER,
                            source_snapshot_id=snapshot_id,
                            table_name=table,
                            column_name=equality.left.name,
                            sql_asset_id=analysis.id,
                            extracted_fact=equality.sql(dialect="postgres"),
                        )
                    )
                time_property_id = next(
                    (
                        f"{object_id}.{infer_property_name(reference.column)}"
                        for reference in analysis.time_fields
                        if reference.table in {None, table}
                        and f"{object_id}.{infer_property_name(reference.column)}"
                        in properties_by_id
                    ),
                    None,
                )
                supported_dimension_set: set[str] = set()
                for group in statement.find_all(exp.Group):
                    for expression in group.expressions:
                        for group_column in expression.find_all(exp.Column):
                            group_table = aliases.get(
                                group_column.table, group_column.table
                            ) or table
                            group_object = table_to_object.get(group_table)
                            if not group_object:
                                continue
                            group_property_id = (
                                f"{group_object}.{infer_property_name(group_column.name)}"
                            )
                            dimension_id = dimensions_by_property.get(group_property_id)
                            if dimension_id:
                                supported_dimension_set.add(dimension_id)
                supported_dimensions = sorted(supported_dimension_set)
                ready = bool(supports) and all(
                    not item.conflicting_values and item.occurrence_count >= 2
                    for item in supports
                )
                result.append(
                    CandidateMetric(
                        candidate_id=f"candidate-metric-{snapshot_id}-{metric_id}",
                        metric=MetricDefinition(
                            id=metric_id,
                            name=(alias or f"{aggregation.value.lower()} {prop.name}").replace(
                                "_", " "
                            ),
                            description=f"Review candidate from certified SQL {analysis.id}",
                            measure_property_id=property_id,
                            aggregation=aggregation,
                            filter_predicates=predicates,
                            time_property_id=time_property_id,
                            supported_dimension_ids=supported_dimensions,
                            lifecycle_status=LifecycleStatus.DRAFT,
                            created_at=prop.created_at,
                            updated_at=prop.updated_at,
                        ),
                        source_fact_table=table,
                        review_state="READY" if ready else "NEEDS_REVIEW",
                        filter_support=supports,
                        confidence=0.92 if ready else 0.72,
                        evidence=evidence,
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
        model = self.factory.chat_model().with_structured_output(
            ObjectCandidateLLMOutput,
            method="function_calling",
        )
        return ObjectCandidateLLMOutput.model_validate(model.invoke(prompt))
