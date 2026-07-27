"""Compile published object semantics into the analytical runtime contract."""

from __future__ import annotations

import hashlib
import inspect
import re
from copy import deepcopy

from pydantic import BaseModel, Field

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    BusinessConcept,
    Dimension,
    JoinDefinition,
    Metric,
    MetricAggregation,
    OntologyBundle,
    PhysicalMapping,
    TableAsset,
)

from .models import ConstructionMode, DraftResources, LifecycleStatus, SemanticRole

COMPILER_NAME = "object-semantic-compiler"
COMPILER_VERSION = "1"


def compiler_source_hash() -> str:
    payload = f"{COMPILER_NAME}:{COMPILER_VERSION}\n{inspect.getsource(ObjectSemanticCompiler)}"
    return hashlib.sha256(payload.encode()).hexdigest()


class SemanticCompilation(BaseModel):
    bundle: OntologyBundle
    property_bindings: dict[str, str] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    metric_evidence: list[dict[str, object]] = Field(default_factory=list)
    dimension_evidence: list[dict[str, object]] = Field(default_factory=list)
    join_evidence: list[dict[str, object]] = Field(default_factory=list)
    seed_accessed: bool = False
    fallback_used: bool = False
    legacy_ontology_accessed: bool = False


class ObjectSemanticCompiler:
    """Treat published object bindings as the authoritative physical source."""

    def __init__(
        self,
        fallback_bundle: OntologyBundle | None,
        construction_mode: ConstructionMode = ConstructionMode.LEGACY_COMPAT,
    ) -> None:
        self.fallback_bundle = fallback_bundle
        self.construction_mode = construction_mode

    @staticmethod
    def _empty_bundle() -> OntologyBundle:
        return OntologyBundle(
            domain={"id": "construction", "name": "Strict construction"},
            concepts=[],
            metrics=[],
            dimensions=[],
            mappings=[],
            joins=[],
            tables=[],
            policies={},
            glossary={},
        )

    @staticmethod
    def _aggregation_expression(aggregation: MetricAggregation, reference: str) -> str:
        if aggregation == MetricAggregation.COUNT_DISTINCT:
            return f"COUNT(DISTINCT {reference})"
        return f"{aggregation.value}({reference})"

    @staticmethod
    def _replace_mapping(items: list[PhysicalMapping], item: PhysicalMapping) -> None:
        items[:] = [existing for existing in items if existing.concept_id != item.concept_id]
        items.append(item)

    @staticmethod
    def _legacy_metric(metric: Metric, mappings: dict[str, PhysicalMapping]) -> Metric:
        mapping = mappings.get(f"metric:{metric.id}")
        if mapping is None:
            return metric
        expression = metric.expression
        for role in set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", expression)):
            column = mapping.column_for(role)
            if column:
                expression = expression.replace(f"{{{role}}}", f"{mapping.table}.{column}")
        filters = {
            mapping.column_for(role) or role: value
            for role, value in metric.required_filters.items()
        }
        return metric.model_copy(
            update={
                "base_table": mapping.table,
                "expression": expression,
                "required_filters": filters,
            }
        )

    def compile(
        self,
        resources: DraftResources,
        *,
        strict_compatibility: bool = True,
        construction_mode: ConstructionMode | None = None,
    ) -> SemanticCompilation:
        mode = construction_mode or self.construction_mode
        strict = mode == ConstructionMode.STRICT_CONSTRUCTION
        if not resources.object_types or not resources.bindings:
            if strict:
                raise OntologyError(
                    "STRICT_CONSTRUCTION requires ObjectType and ObjectDataSourceBinding resources"
                )
            if self.fallback_bundle is None:
                raise OntologyError("Legacy fallback bundle is not configured")
            return SemanticCompilation(
                bundle=deepcopy(self.fallback_bundle),
                fallback_used=True,
                legacy_ontology_accessed=True,
            )

        if strict:
            missing: list[str] = []
            if not resources.metrics:
                missing.append("MetricDefinition")
            if not resources.dimensions:
                missing.append("DimensionDefinition")
            if len(resources.object_types) > 1 and not resources.physical_joins:
                missing.append("PhysicalJoinDefinition")
            if missing:
                raise OntologyError(
                    "STRICT_CONSTRUCTION missing required resources: " + ", ".join(missing)
                )

        if strict:
            bundle = self._empty_bundle()
        else:
            if self.fallback_bundle is None:
                raise OntologyError("Legacy fallback bundle is not configured")
            bundle = deepcopy(self.fallback_bundle)
        active_objects = {
            item.id: item
            for item in resources.object_types
            if item.lifecycle_status == LifecycleStatus.ACTIVE
        }
        properties = {
            item.id: item
            for item in resources.properties
            if item.lifecycle_status == LifecycleStatus.ACTIVE
        }
        bindings_by_object = {
            item.object_type_id: item
            for item in resources.bindings
            if item.object_type_id in active_objects
        }
        property_locations: dict[str, tuple[str, str]] = {}
        for object_id, binding in bindings_by_object.items():
            role_bindings: dict[str, str] = {}
            for property_id, column in binding.property_bindings.items():
                if property_id not in properties:
                    continue
                property_locations[property_id] = (binding.table_name, column)
                role = property_id.rsplit(".", 1)[-1]
                role_bindings[role] = column
                role_bindings[column] = column
            self._replace_mapping(
                bundle.mappings,
                PhysicalMapping(
                    concept_id=object_id,
                    table=binding.table_name,
                    column_bindings=role_bindings,
                ),
            )

        draft_owns_analytical_semantics = strict or bool(
            resources.metrics or resources.dimensions
        )
        dimensions_by_property: dict[str, str] = {}
        dimensions_by_id: dict[str, Dimension] = {}
        compiled_dimensions: list[Dimension] = []
        dimension_evidence: list[dict[str, object]] = []
        conflicts: list[str] = []
        dimension_sources = (
            [
                Dimension(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    table="",
                    column="",
                    synonyms=item.synonyms,
                    property_id=item.property_id,
                )
                for item in resources.dimensions
                if item.lifecycle_status == LifecycleStatus.ACTIVE
            ]
            if draft_owns_analytical_semantics
            else bundle.dimensions
        )
        for dimension in dimension_sources:
            if not dimension.property_id:
                if draft_owns_analytical_semantics:
                    raise OntologyError(f"Dimension {dimension.id} has no Property reference")
                compiled_dimensions.append(dimension)
                dimensions_by_id[dimension.id] = dimension
                continue
            location = property_locations.get(dimension.property_id)
            if location is None:
                raise OntologyError(
                    f"Dimension {dimension.id} references unbound property "
                    f"{dimension.property_id}"
                )
            table, column = location
            if (
                not draft_owns_analytical_semantics
                and strict_compatibility
                and (dimension.table, dimension.column) != location
            ):
                conflicts.append(
                    f"Dimension {dimension.id}: legacy {dimension.table}.{dimension.column} "
                    f"conflicts with {dimension.property_id} -> {table}.{column}"
                )
            compiled = dimension.model_copy(update={"table": table, "column": column})
            compiled_dimensions.append(compiled)
            dimension_evidence.append(
                {
                    "dimension_id": dimension.id,
                    "property_id": dimension.property_id,
                    "resolved_table": table,
                    "resolved_column": column,
                }
            )
            dimensions_by_id[dimension.id] = compiled
            dimensions_by_property[dimension.property_id] = dimension.id
            self._replace_mapping(
                bundle.mappings,
                PhysicalMapping(
                    concept_id=f"dimension:{dimension.id}",
                    table=table,
                    column_bindings={"value": column},
                ),
            )
        bundle.dimensions = compiled_dimensions

        legacy_mappings = (
            {}
            if strict or self.fallback_bundle is None
            else {item.concept_id: item for item in self.fallback_bundle.mappings}
        )
        metric_sources = (
            [
                Metric(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    expression="",
                    base_table="",
                    synonyms=item.synonyms,
                    measure_property_id=item.measure_property_id,
                    aggregation=item.aggregation,
                    filter_predicates=item.filter_predicates,
                    time_property_id=item.time_property_id,
                    supported_dimensions=item.supported_dimension_ids,
                    supported_dimension_property_ids=[
                        dimensions_by_id[dimension_id].property_id
                        for dimension_id in item.supported_dimension_ids
                        if dimension_id in dimensions_by_id
                        and dimensions_by_id[dimension_id].property_id
                    ],
                )
                for item in resources.metrics
                if item.lifecycle_status == LifecycleStatus.ACTIVE
            ]
            if draft_owns_analytical_semantics
            else bundle.metrics
        )
        compiled_metrics: list[Metric] = []
        metric_evidence: list[dict[str, object]] = []
        for metric in metric_sources:
            if not metric.measure_property_id or not metric.aggregation:
                if draft_owns_analytical_semantics:
                    raise OntologyError(
                        f"Metric {metric.id} requires measure_property_id and aggregation"
                    )
                compiled_metrics.append(metric)
                continue
            measure = property_locations.get(metric.measure_property_id)
            if measure is None:
                raise OntologyError(
                    f"Metric {metric.id} references unbound property "
                    f"{metric.measure_property_id}"
                )
            table, column = measure
            measure_property = properties[metric.measure_property_id]
            if (
                metric.aggregation
                in {
                    MetricAggregation.SUM,
                    MetricAggregation.AVG,
                    MetricAggregation.MIN,
                    MetricAggregation.MAX,
                }
                and measure_property.semantic_role != SemanticRole.MEASURE
            ):
                raise OntologyError(
                    f"Metric {metric.id} aggregation {metric.aggregation.value} requires "
                    f"a MEASURE property, got {measure_property.semantic_role.value}"
                )
            expression = self._aggregation_expression(
                metric.aggregation, f"{table}.{column}"
            )
            required_filters: dict[str, str] = {}
            metric_bindings = {column: column}
            metric_bindings[metric.measure_property_id.rsplit(".", 1)[-1]] = column
            for predicate in metric.filter_predicates:
                if predicate.operator != "EQ" or isinstance(predicate.value, list):
                    raise OntologyError(
                        f"Metric {metric.id} compatibility projection supports EQ scalar "
                        "required filters only"
                    )
                filter_location = property_locations.get(predicate.property_id)
                if filter_location is None or filter_location[0] != table:
                    raise OntologyError(
                        f"Metric {metric.id} filter property {predicate.property_id} "
                        "is unbound or belongs to another fact table"
                    )
                filter_column = filter_location[1]
                required_filters[filter_column] = predicate.value
                metric_bindings[predicate.property_id.rsplit(".", 1)[-1]] = filter_column
                metric_bindings[filter_column] = filter_column
            time_dimension = metric.time_dimension
            if metric.time_property_id:
                time_location = property_locations.get(metric.time_property_id)
                if time_location is None or time_location[0] != table:
                    raise OntologyError(
                        f"Metric {metric.id} time property is not bound to its fact table"
                    )
                time_dimension = dimensions_by_property.get(metric.time_property_id)
                if time_dimension is None:
                    raise OntologyError(
                        f"Metric {metric.id} time property has no published Dimension"
                    )
                metric_bindings[metric.time_property_id.rsplit(".", 1)[-1]] = time_location[1]
                metric_bindings[time_location[1]] = time_location[1]
            if draft_owns_analytical_semantics:
                missing_dimensions = set(metric.supported_dimensions) - dimensions_by_id.keys()
                if missing_dimensions:
                    raise OntologyError(
                        f"Metric {metric.id} references missing or inactive Dimensions: "
                        f"{sorted(missing_dimensions)}"
                    )
                supported_dimensions = list(metric.supported_dimensions)
            else:
                supported_dimensions = [
                    dimensions_by_property[property_id]
                    for property_id in metric.supported_dimension_property_ids
                    if property_id in dimensions_by_property
                ]
            compiled = metric.model_copy(
                update={
                    "base_table": table,
                    "expression": expression,
                    "required_filters": required_filters,
                    "time_dimension": time_dimension,
                    "supported_dimensions": supported_dimensions,
                }
            )
            legacy = self._legacy_metric(metric, legacy_mappings)
            if not draft_owns_analytical_semantics and strict_compatibility and (
                legacy.base_table != compiled.base_table
                or legacy.expression != compiled.expression
                or legacy.required_filters != compiled.required_filters
                or legacy.time_dimension != compiled.time_dimension
                or legacy.supported_dimensions != compiled.supported_dimensions
            ):
                conflicts.append(
                    f"Metric {metric.id}: legacy physical contract conflicts with "
                    f"object property binding for {metric.measure_property_id}"
                )
            compiled_metrics.append(compiled)
            metric_evidence.append(
                {
                    "metric_id": metric.id,
                    "measure_property_id": metric.measure_property_id,
                    "aggregation": metric.aggregation.value,
                    "resolved_table": table,
                    "resolved_column": column,
                    "compiled_expression": expression,
                    "required_filters": required_filters,
                    "time_dimension": time_dimension,
                    "supported_dimensions": supported_dimensions,
                }
            )
            self._replace_mapping(
                bundle.mappings,
                PhysicalMapping(
                    concept_id=f"metric:{metric.id}",
                    table=table,
                    column_bindings=metric_bindings,
                ),
            )
        bundle.metrics = compiled_metrics

        bundle.joins = [
            JoinDefinition(
                left_table=join.left_table,
                left_column=join.left_column,
                right_table=join.right_table,
                right_column=join.right_column,
                relationship=join.relationship,
                enabled=join.enabled,
            )
            for join in resources.physical_joins
            if join.enabled and join.lifecycle_status == LifecycleStatus.ACTIVE
        ]
        physical_joins = {item.id: item for item in resources.physical_joins}
        join_evidence = [
            {
                "link_id": link.id,
                "physical_join_id": join_id,
                "left_endpoint": (
                    f"{physical_joins[join_id].left_table}."
                    f"{physical_joins[join_id].left_column}"
                ),
                "right_endpoint": (
                    f"{physical_joins[join_id].right_table}."
                    f"{physical_joins[join_id].right_column}"
                ),
            }
            for link in resources.link_types
            for join_id in link.physical_join_ids
            if join_id in physical_joins
        ]
        if strict:
            bundle.tables = [
                TableAsset(
                    name=binding.table_name,
                    description=f"Strict construction binding for {binding.object_type_id}",
                    status="ACTIVE",
                    selectable=True,
                    grain=binding.primary_key_column,
                    columns=sorted(binding.schema_columns),
                    tags=["constructed"],
                )
                for binding in bindings_by_object.values()
            ]
            bundle.concepts = [
                BusinessConcept(
                    id=item.id,
                    name=item.name,
                    kind="object",
                    description=item.description,
                    synonyms=item.synonyms,
                )
                for item in active_objects.values()
            ] + [
                BusinessConcept(
                    id=item.id,
                    name=item.name,
                    kind="dimension",
                    description=item.description,
                    synonyms=item.synonyms,
                )
                for item in compiled_dimensions
            ] + [
                BusinessConcept(
                    id=item.id,
                    name=item.name,
                    kind="metric",
                    description=item.description,
                    synonyms=item.synonyms,
                )
                for item in compiled_metrics
            ]
        return SemanticCompilation(
            bundle=bundle,
            property_bindings={
                property_id: f"{table}.{column}"
                for property_id, (table, column) in property_locations.items()
            },
            conflicts=conflicts,
            metric_evidence=metric_evidence,
            dimension_evidence=dimension_evidence,
            join_evidence=join_evidence,
            seed_accessed=False,
            fallback_used=not strict and not draft_owns_analytical_semantics,
            legacy_ontology_accessed=not strict,
        )
