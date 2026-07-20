"""Compatibility projection from published object resources to legacy consumers."""

from copy import deepcopy

from data_asset_agents.ontology.models import BusinessConcept, OntologyBundle

from .compiler import ObjectSemanticCompiler
from .models import DraftResources, LifecycleStatus


class CompatibilityProjectionService:
    """Supplement—not replace—the stable analytical ontology contract."""

    def project(self, base: OntologyBundle, resources: DraftResources) -> OntologyBundle:
        bundle = ObjectSemanticCompiler(deepcopy(base)).compile(
            resources, strict_compatibility=False
        ).bundle
        concept_ids = {item.id for item in bundle.concepts}
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
        return bundle
