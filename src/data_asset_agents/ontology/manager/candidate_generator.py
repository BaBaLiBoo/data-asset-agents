"""Object-first candidates layered over the existing field-level evidence."""

from __future__ import annotations

import json

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import MetadataSnapshot, OntologyBundle

from .classifier import TableRoleClassifier
from .migration import LegacyOntologyObjectMigrator
from .models import (
    CandidateEvidence,
    CandidateLinkType,
    CandidateObjectBinding,
    CandidateObjectType,
    CandidateProperty,
    ObjectCandidateLLMOutput,
)


class ObjectFirstCandidateGenerator:
    """Generate review-only object/property/link/binding candidates in dependency order."""

    def __init__(
        self,
        settings: Settings,
        bundle: OntologyBundle,
        factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings
        self.bundle = bundle
        self.factory = factory or ModelFactory(settings)
        self.classifier = TableRoleClassifier()

    def generate(
        self, snapshot: MetadataSnapshot
    ) -> tuple[
        list[CandidateObjectType],
        list[CandidateProperty],
        list[CandidateLinkType],
        list[CandidateObjectBinding],
    ]:
        resources = LegacyOntologyObjectMigrator(self.bundle).migrate(snapshot.id)
        snapshot_tables = {item.table_name: item for item in snapshot.tables}
        table_assets = {item.name: item for item in self.bundle.tables}
        binding_by_object = {item.object_type_id: item for item in resources.bindings}
        objects: list[CandidateObjectType] = []
        properties: list[CandidateProperty] = []
        bindings: list[CandidateObjectBinding] = []
        for obj in resources.object_types:
            binding = binding_by_object[obj.id]
            asset = table_assets[binding.table_name]
            role, role_evidence = self.classifier.classify(asset)
            table = snapshot_tables.get(binding.table_name)
            evidence = [
                CandidateEvidence(source="table-role", detail=item) for item in role_evidence
            ]
            if table:
                evidence.extend(
                    [
                        CandidateEvidence(
                            source="metadata",
                            detail=f"primary_key={table.primary_key}",
                        ),
                        CandidateEvidence(
                            source="metadata",
                            detail=f"foreign_keys={len(table.foreign_keys)}",
                        ),
                    ]
                )
            confidence = 0.97
            candidate_obj = obj
            if self.settings.llm_mode != "mock" and table is not None:
                output = self._live_output(obj.id, table.model_dump(mode="json"))
                candidate_obj = obj.model_copy(
                    update={
                        "name": output.object_name,
                        "description": output.boundary_description,
                    }
                )
                confidence = output.confidence
                evidence.extend(
                    CandidateEvidence(source="llm", detail=item) for item in output.evidence
                )
            objects.append(
                CandidateObjectType(
                    candidate_id=f"candidate-object-{snapshot.id}-{obj.id}",
                    object_type=candidate_obj,
                    table_role=role,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
            for prop in resources.properties:
                if prop.object_type_id == obj.id:
                    properties.append(
                        CandidateProperty(
                            candidate_id=f"candidate-property-{snapshot.id}-{prop.id}",
                            property=prop,
                            confidence=0.96 if prop.id in binding.property_bindings else 0.55,
                            evidence=[
                                CandidateEvidence(
                                    source="field-profile",
                                    detail=(
                                        f"physical_column="
                                        f"{binding.property_bindings.get(prop.id, 'unbound')}"
                                    ),
                                )
                            ],
                        )
                    )
            bindings.append(
                CandidateObjectBinding(
                    candidate_id=f"candidate-binding-{snapshot.id}-{binding.id}",
                    binding=binding,
                    confidence=0.98,
                    evidence=[
                        CandidateEvidence(source="reviewed-mapping", detail=binding.table_name)
                    ],
                )
            )
        links = [
            CandidateLinkType(
                candidate_id=f"candidate-link-{snapshot.id}-{link.id}",
                link_type=link,
                confidence=0.98,
                evidence=[
                    CandidateEvidence(
                        source="reviewed-physical-join",
                        detail=", ".join(link.physical_join_ids),
                    )
                ],
            )
            for link in resources.link_types
        ]
        return objects, properties, links, bindings

    def _live_output(self, object_id: str, table: dict[str, object]) -> ObjectCandidateLLMOutput:
        prompt = (
            "Generate a review-only business object candidate from masked metadata. "
            "Do not create connections, physical SQL, actions, or publication commands.\n"
            + json.dumps(
                {"object_hint": object_id, "metadata": table},
                ensure_ascii=False,
            )
        )
        model = self.factory.chat_model().with_structured_output(ObjectCandidateLLMOutput)
        return ObjectCandidateLLMOutput.model_validate(model.invoke(prompt))
