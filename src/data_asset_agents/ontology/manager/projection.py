"""Compatibility projection from published object resources to legacy consumers."""

from copy import deepcopy

from data_asset_agents.ontology.models import (
    BusinessConcept,
    JoinDefinition,
    OntologyBundle,
    PhysicalMapping,
)

from .models import DraftResources, LifecycleStatus


class CompatibilityProjectionService:
    """Supplement—not replace—the stable analytical ontology contract."""

    def project(self, base: OntologyBundle, resources: DraftResources) -> OntologyBundle:
        bundle = deepcopy(base)
        concept_ids = {item.id for item in bundle.concepts}
        mapping_ids = {item.concept_id for item in bundle.mappings}
        bindings_by_object = {item.object_type_id: item for item in resources.bindings}
        for object_type in resources.object_types:
            if object_type.lifecycle_status != LifecycleStatus.ACTIVE:
                continue
            if object_type.id not in concept_ids:
                bundle.concepts.append(
                    BusinessConcept(
                        id=object_type.id,
                        name=object_type.name,
                        kind="object",
                        description=object_type.description,
                        synonyms=object_type.synonyms,
                    )
                )
            binding = bindings_by_object.get(object_type.id)
            if binding and object_type.id not in mapping_ids:
                bundle.mappings.append(
                    PhysicalMapping(
                        concept_id=object_type.id,
                        table=binding.table_name,
                        column_bindings={
                            property_id.rsplit(".", 1)[-1]: column
                            for property_id, column in binding.property_bindings.items()
                        },
                    )
                )
        known = {(j.left_table, j.left_column, j.right_table, j.right_column) for j in bundle.joins}
        active_join_ids = {
            join_id
            for link in resources.link_types
            if link.lifecycle_status == LifecycleStatus.ACTIVE
            for join_id in link.physical_join_ids
        }
        for join in resources.physical_joins:
            key = (join.left_table, join.left_column, join.right_table, join.right_column)
            if join.id in active_join_ids and join.enabled and key not in known:
                bundle.joins.append(
                    JoinDefinition(
                        left_table=join.left_table,
                        left_column=join.left_column,
                        right_table=join.right_table,
                        right_column=join.right_column,
                        relationship=join.relationship,
                        enabled=True,
                    )
                )
        return bundle
