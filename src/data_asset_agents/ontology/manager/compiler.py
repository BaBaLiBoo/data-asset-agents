"""Compile published object semantics into the analytical runtime contract."""

from __future__ import annotations

import re
from copy import deepcopy

from pydantic import BaseModel, Field

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    Dimension,
    JoinDefinition,
    Metric,
    MetricAggregation,
    OntologyBundle,
    PhysicalMapping,
)

from .models import DraftResources, LifecycleStatus, SemanticRole


class SemanticCompilation(BaseModel):
    bundle: OntologyBundle
    property_bindings: dict[str, str] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)


class ObjectSemanticCompiler:
    """Treat published object bindings as the authoritative physical source."""

    def __init__(self, fallback_bundle: OntologyBundle) -> None:
        self.fallback_bundle = fallback_bundle

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
    ) -> SemanticCompilation:
        if not resources.object_types or not resources.bindings:
            return SemanticCompilation(bundle=deepcopy(self.fallback_bundle))

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

        dimensions_by_property: dict[str, str] = {}
        compiled_dimensions: list[Dimension] = []
        conflicts: list[str] = []
        for dimension in bundle.dimensions:
            if not dimension.property_id:
                compiled_dimensions.append(dimension)
                continue
            location = property_locations.get(dimension.property_id)
            if location is None:
                raise OntologyError(
                    f"Dimension {dimension.id} references unbound property "
                    f"{dimension.property_id}"
                )
            table, column = location
            if strict_compatibility and (dimension.table, dimension.column) != location:
                conflicts.append(
                    f"Dimension {dimension.id}: legacy {dimension.table}.{dimension.column} "
                    f"conflicts with {dimension.property_id} -> {table}.{column}"
                )
            compiled = dimension.model_copy(update={"table": table, "column": column})
            compiled_dimensions.append(compiled)
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

        legacy_mappings = {
            item.concept_id: item for item in self.fallback_bundle.mappings
        }
        compiled_metrics: list[Metric] = []
        for metric in bundle.metrics:
            if not metric.measure_property_id or not metric.aggregation:
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
            if strict_compatibility and (
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
        return SemanticCompilation(
            bundle=bundle,
            property_bindings={
                property_id: f"{table}.{column}"
                for property_id, (table, column) in property_locations.items()
            },
            conflicts=conflicts,
        )
