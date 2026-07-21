from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.api.main import app
from data_asset_agents.core.errors import (
    OntologyConflictError,
    OntologyError,
    OntologyGovernanceError,
)
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.hashing import (
    calculate_bundle_hash,
    calculate_draft_resource_hash,
)
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CandidateProperty,
    CompiledArtifactStatus,
    CreateDraftFromSeedRequest,
    DraftStatus,
    ImportObjectCandidatesRequest,
    ObjectCandidateSet,
    OntologyAuditAction,
    OntologyAuditEvent,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    SemanticRole,
    ValidationState,
)
from data_asset_agents.ontology.manager.repository import MemoryOntologyManagerRepository
from data_asset_agents.ontology.manager.seed_repository import ObjectOntologySeedRepository
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.models import MetadataSnapshot
from data_asset_agents.ontology.repository import YamlOntologyRepository


@pytest.fixture
def bundle():
    return YamlOntologyRepository("ontology/retail_banking").load()


@pytest.fixture
def service(bundle):
    repository = MemoryOntologyManagerRepository()
    return OntologyManagerService(
        repository,
        bundle,
        OntologyDraftValidator(bundle),
        seed_repository=ObjectOntologySeedRepository(
            {"retail_banking": Path("ontology/retail_banking/object_model")}
        ),
    )


def _seed(service: OntologyManagerService):
    return service.create_draft_from_seed(CreateDraftFromSeedRequest(created_by="test"))


def _publish(service: OntologyManagerService, version: str = "governance-v1"):
    draft = _seed(service)
    service.validate(draft.draft.id, expected_revision=draft.draft.resource_revision)
    service.submit(
        draft.draft.id,
        ActorRequest(actor="author"),
        expected_revision=draft.draft.resource_revision,
    )
    service.approve(
        draft.draft.id,
        ActorRequest(actor="reviewer"),
        expected_revision=draft.draft.resource_revision,
    )
    published = service.publish(
        draft.draft.id,
        PublishDraftRequest(actor="reviewer", version=version),
        expected_revision=draft.draft.resource_revision,
    )
    return draft, published


def test_resource_hash_is_order_independent_and_ignores_runtime_timestamps(service) -> None:
    draft = _seed(service)
    reordered = draft.resources.model_copy(deep=True)
    for name in type(reordered).model_fields:
        value = getattr(reordered, name)
        if isinstance(value, list):
            value.reverse()
    assert calculate_draft_resource_hash(reordered) == draft.draft.resource_hash

    changed_time = draft.resources.model_copy(deep=True)
    changed_time.object_types[0].updated_at = datetime.now(UTC) + timedelta(days=10)
    changed_time.bindings[0].last_inspected_at = datetime.now(UTC) + timedelta(days=10)
    changed_time.bindings[0].sync_status = "FAILED"
    assert calculate_draft_resource_hash(changed_time) == draft.draft.resource_hash


def test_description_changes_hash_noop_does_not_increment_and_mutation_stales(service) -> None:
    draft = _seed(service)
    original = draft.resources.metrics[0]
    noop = service.save_resource(
        draft.draft.id, original, expected_revision=draft.draft.resource_revision
    )
    assert noop.draft.resource_revision == draft.draft.resource_revision
    assert noop.draft.resource_hash == draft.draft.resource_hash

    service.validate(draft.draft.id, expected_revision=draft.draft.resource_revision)
    changed = original.model_copy(update={"description": original.description + " clarified"})
    updated = service.save_resource(
        draft.draft.id, changed, expected_revision=draft.draft.resource_revision
    )
    assert updated.draft.resource_revision == draft.draft.resource_revision + 1
    assert updated.draft.resource_hash != draft.draft.resource_hash
    assert updated.draft.validation_state == ValidationState.STALE
    assert updated.draft.validation_report is None
    assert updated.draft.validated_revision is None


def test_submit_requires_exact_successful_validation(service) -> None:
    draft = _seed(service)
    with pytest.raises(OntologyGovernanceError) as missing:
        service.submit(draft.draft.id, ActorRequest())
    assert missing.value.code == "DRAFT_NOT_VALIDATED"

    validated = service.validate(draft.draft.id)
    metric = validated.resources.metrics[0].model_copy(update={"description": "changed"})
    stale = service.save_resource(
        draft.draft.id,
        metric,
        expected_revision=validated.draft.resource_revision,
    )
    with pytest.raises(OntologyGovernanceError) as changed:
        service.submit(
            draft.draft.id,
            ActorRequest(),
            expected_revision=stale.draft.resource_revision,
        )
    assert changed.value.code in {"DRAFT_NOT_VALIDATED", "DRAFT_VALIDATION_STALE"}


def test_failed_validation_cannot_submit(service) -> None:
    draft = _seed(service)
    binding = draft.resources.bindings[0]
    invalid = service.delete_resource(
        draft.draft.id,
        "binding",
        binding.id,
        expected_revision=draft.draft.resource_revision,
    )
    failed = service.validate(
        draft.draft.id, expected_revision=invalid.draft.resource_revision
    )
    assert failed.draft.validation_state == ValidationState.FAILED
    assert failed.draft.validation_report is not None
    assert not failed.draft.validation_report.valid
    with pytest.raises(OntologyGovernanceError) as error:
        service.submit(
            draft.draft.id,
            ActorRequest(),
            expected_revision=failed.draft.resource_revision,
        )
    assert error.value.code == "DRAFT_VALIDATION_FAILED"


def test_stale_expected_revision_and_two_concurrent_updates_allow_only_one(service) -> None:
    draft = _seed(service)
    revision = draft.draft.resource_revision
    first = draft.resources.metrics[0].model_copy(update={"description": "first"})
    second = draft.resources.metrics[0].model_copy(update={"description": "second"})

    def update(resource):
        try:
            service.save_resource(
                draft.draft.id, resource, expected_revision=revision, actor="concurrent"
            )
            return "success"
        except OntologyConflictError as exc:
            assert exc.code == "DRAFT_REVISION_CONFLICT"
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, [first, second]))
    assert sorted(outcomes) == ["conflict", "success"]
    assert service.get_draft(draft.draft.id).draft.resource_revision == revision + 1


def test_lifecycle_and_draft_delete_reject_stale_revision(service) -> None:
    draft = _seed(service)
    with pytest.raises(OntologyConflictError) as validation_conflict:
        service.validate(
            draft.draft.id,
            expected_revision=draft.draft.resource_revision + 1,
        )
    assert validation_conflict.value.code == "DRAFT_REVISION_CONFLICT"
    assert validation_conflict.value.current_revision == draft.draft.resource_revision

    with pytest.raises(OntologyConflictError) as delete_conflict:
        service.delete_draft(
            draft.draft.id,
            expected_revision=draft.draft.resource_revision + 1,
        )
    assert delete_conflict.value.code == "DRAFT_REVISION_CONFLICT"
    assert service.get_draft(draft.draft.id).draft.id == draft.draft.id


def test_cascade_delete_is_one_revision(service) -> None:
    draft = _seed(service)
    deleted = service.delete_resource(
        draft.draft.id,
        "object_type",
        "merchant",
        expected_revision=draft.draft.resource_revision,
    )
    assert deleted.draft.resource_revision == draft.draft.resource_revision + 1
    assert all(item.id != "merchant" for item in deleted.resources.object_types)
    assert all(item.object_type_id != "merchant" for item in deleted.resources.properties)


def test_candidate_batch_import_is_one_revision(bundle) -> None:
    candidates = [
        CandidateProperty(
            candidate_id=f"candidate-{suffix}",
            property=PropertyDefinition(
                id=f"transaction.{suffix}",
                object_type_id="transaction",
                name=suffix,
                data_type=PropertyDataType.STRING,
                semantic_role=SemanticRole.ATTRIBUTE,
            ),
            confidence=0.9,
        )
        for suffix in ("review_note", "review_source")
    ]

    class CandidateRepository:
        @staticmethod
        def get_metadata_snapshot(snapshot_id):
            return MetadataSnapshot(id=snapshot_id, schema_name="public", tables=[])

        @staticmethod
        def list_historical_sql(snapshot_id):
            return []

        @staticmethod
        def list_candidates(**kwargs):
            return []

    class Generator:
        @staticmethod
        def generate(snapshot, historical_sql, existing):
            return ObjectCandidateSet(snapshot_id=snapshot.id, properties=candidates)

    batch_service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
        candidate_repository=CandidateRepository(),
        candidate_generator=Generator(),  # type: ignore[arg-type]
        seed_repository=ObjectOntologySeedRepository(
            {"retail_banking": Path("ontology/retail_banking/object_model")}
        ),
    )
    draft = batch_service.create_draft_from_seed(
        CreateDraftFromSeedRequest(source_snapshot_id="batch-snapshot")
    )
    imported = batch_service.import_candidates(
        draft.draft.id,
        ImportObjectCandidatesRequest(
            candidate_ids=[item.candidate_id for item in candidates], actor="reviewer"
        ),
        expected_revision=draft.draft.resource_revision,
    )
    assert imported.draft.resource_revision == draft.draft.resource_revision + 1
    assert {item.property.id for item in candidates} <= {
        item.id for item in imported.resources.properties
    }


def test_validation_result_is_not_saved_if_snapshot_changes(service, monkeypatch) -> None:
    draft = _seed(service)
    original_validate = service.validator.validate

    def mutate_during_validation(resources, snapshot_id):
        metric = service.get_draft(draft.draft.id).resources.metrics[0]
        service.save_resource(
            draft.draft.id,
            metric.model_copy(update={"description": "concurrent mutation"}),
            expected_revision=draft.draft.resource_revision,
        )
        return original_validate(resources, snapshot_id)

    monkeypatch.setattr(service.validator, "validate", mutate_during_validation)
    with pytest.raises(OntologyGovernanceError) as changed:
        service.validate(draft.draft.id, expected_revision=draft.draft.resource_revision)
    assert changed.value.code == "DRAFT_CHANGED_DURING_VALIDATION"
    current = service.get_draft(draft.draft.id).draft
    assert current.validation_report is None
    assert current.validation_state == ValidationState.STALE


def test_submit_locks_revision_hash_and_changed_review_snapshot_is_rejected(service) -> None:
    draft = _seed(service)
    service.validate(draft.draft.id)
    submitted = service.submit(draft.draft.id, ActorRequest(actor="author"))
    assert submitted.draft.submitted_revision == submitted.draft.resource_revision
    assert submitted.draft.submitted_hash == submitted.draft.resource_hash
    assert submitted.draft.status == DraftStatus.IN_REVIEW

    repository = service.repository
    stored = repository.drafts[draft.draft.id]  # type: ignore[attr-defined]
    stored.resources.metrics[0].description = "tampered review content"
    stored.draft.resource_hash = calculate_draft_resource_hash(stored.resources)
    with pytest.raises(OntologyGovernanceError) as changed:
        service.approve(draft.draft.id, ActorRequest(actor="reviewer"))
    assert changed.value.code == "REVIEW_SNAPSHOT_CHANGED"


def test_publish_rejects_content_outside_the_approved_snapshot(service) -> None:
    draft = _seed(service)
    service.validate(draft.draft.id)
    service.submit(draft.draft.id, ActorRequest(actor="author"))
    service.approve(draft.draft.id, ActorRequest(actor="reviewer"))

    repository = service.repository
    stored = repository.drafts[draft.draft.id]  # type: ignore[attr-defined]
    stored.resources.metrics[0].description = "tampered after approval"
    stored.draft.resource_hash = calculate_draft_resource_hash(stored.resources)
    with pytest.raises(OntologyGovernanceError) as changed:
        service.publish(
            draft.draft.id,
            PublishDraftRequest(actor="reviewer", version="tampered-review-v1"),
        )
    assert changed.value.code == "REVIEW_SNAPSHOT_CHANGED"
    assert repository.current[0] is None  # type: ignore[attr-defined]


def test_publish_persists_stable_ready_artifact_and_compilation_evidence(service) -> None:
    draft, version = _publish(service)
    artifact = service.repository.get_compiled_artifact(version.id)
    assert artifact is not None
    assert artifact.source_revision == draft.draft.resource_revision
    assert artifact.source_resource_hash == draft.draft.resource_hash
    assert artifact.bundle_hash == calculate_bundle_hash(artifact.bundle_json)
    assert artifact.property_bindings
    assert artifact.metric_compilation_evidence
    assert artifact.dimension_compilation_evidence
    assert artifact.join_compilation_evidence
    repeated = ObjectSemanticCompiler(service.base_bundle).compile(draft.resources)
    repeated.bundle.domain["version"] = version.version
    assert calculate_bundle_hash(repeated.bundle) == artifact.bundle_hash


def test_artifact_failure_leaves_version_and_draft_unpublished(bundle) -> None:
    class FailingArtifactRepository(MemoryOntologyManagerRepository):
        def publish(self, *_args, **_kwargs) -> None:
            raise OntologyError("injected artifact persistence failure")

    repository = FailingArtifactRepository()
    failing_service = OntologyManagerService(
        repository,
        bundle,
        OntologyDraftValidator(bundle),
        seed_repository=ObjectOntologySeedRepository(
            {"retail_banking": Path("ontology/retail_banking/object_model")}
        ),
    )
    draft = _seed(failing_service)
    failing_service.validate(draft.draft.id)
    failing_service.submit(draft.draft.id, ActorRequest(actor="author"))
    failing_service.approve(draft.draft.id, ActorRequest(actor="reviewer"))
    with pytest.raises(OntologyError, match="injected artifact"):
        failing_service.publish(
            draft.draft.id,
            PublishDraftRequest(actor="reviewer", version="failed-artifact-v1"),
        )
    assert repository.current[0] is None
    assert repository.artifacts == {}
    assert repository.drafts[draft.draft.id].draft.status == DraftStatus.VALIDATED


def test_ready_artifact_runtime_does_not_call_compiler(service, monkeypatch) -> None:
    _, version = _publish(service)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("published artifact must not be recompiled")

    monkeypatch.setattr(ObjectSemanticCompiler, "compile", forbidden)
    bundle, legacy, artifact = service.load_runtime_bundle(version.id, service.base_bundle)
    assert not legacy
    assert artifact is not None
    assert calculate_bundle_hash(bundle) == artifact.bundle_hash


def test_failed_artifact_is_not_treated_as_a_legacy_version(service, monkeypatch) -> None:
    _, version = _publish(service)
    repository = service.repository
    repository.artifacts[version.id].status = CompiledArtifactStatus.FAILED  # type: ignore[attr-defined]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("FAILED artifact must not trigger compatibility compilation")

    monkeypatch.setattr(ObjectSemanticCompiler, "compile", forbidden)
    with pytest.raises(OntologyGovernanceError) as failed:
        service.load_runtime_bundle(version.id, service.base_bundle)
    assert failed.value.code == "ONTOLOGY_ARTIFACT_NOT_READY"


def test_legacy_version_without_artifact_uses_compatibility_compile(service) -> None:
    draft = _seed(service)
    repository = service.repository
    repository.versions["legacy-version"] = deepcopy(draft.resources)  # type: ignore[attr-defined]
    bundle, legacy, artifact = service.load_runtime_bundle(
        "legacy-version", service.base_bundle
    )
    assert legacy
    assert artifact is None
    assert bundle.metrics


def test_tampered_artifact_bundle_or_resource_hash_blocks_activation(service) -> None:
    _, version = _publish(service)
    repository = service.repository
    artifact = repository.artifacts[version.id]  # type: ignore[attr-defined]
    artifact.bundle_json["domain"]["name"] = "tampered"  # type: ignore[index]
    with pytest.raises(OntologyGovernanceError) as bundle_error:
        service.load_runtime_bundle(version.id, service.base_bundle)
    assert bundle_error.value.code == "ONTOLOGY_ARTIFACT_BUNDLE_HASH_MISMATCH"

    artifact.bundle_hash = calculate_bundle_hash(artifact.bundle_json)
    artifact.source_resource_hash = "0" * 64
    with pytest.raises(OntologyGovernanceError) as resource_error:
        service.load_runtime_bundle(version.id, service.base_bundle)
    assert resource_error.value.code == "ONTOLOGY_ARTIFACT_RESOURCE_HASH_MISMATCH"


def test_audit_events_are_append_only_copies_and_strip_sensitive_metadata(service) -> None:
    draft = _seed(service)
    service.repository.append_audit_event(
        OntologyAuditEvent(
            event_id="audit-sensitive-test",
            draft_id=draft.draft.id,
            actor="test",
            action=OntologyAuditAction.VALIDATION_STARTED,
            metadata={
                "database_url": "postgresql://secret",
                "api_key": "not-for-audit",
                "nested": {
                    "connection_string": "postgresql://nested-secret",
                    "safe_child": "summary-child",
                    "message": "postgresql://also-secret",
                },
                "safe": "summary",
            },
        )
    )
    events = service.audit_events(draft_id=draft.draft.id)
    sensitive = next(item for item in events if item.event_id == "audit-sensitive-test")
    assert sensitive.metadata == {
        "nested": {"safe_child": "summary-child", "message": "[REDACTED]"},
        "safe": "summary",
    }
    events.clear()
    assert service.audit_events(draft_id=draft.draft.id)


def test_openapi_requires_if_match_for_draft_mutations() -> None:
    paths = app.openapi()["paths"]
    operations = [
        paths["/api/v1/ontology/drafts/{draft_id}"]["delete"],
        paths["/api/v1/ontology/drafts/{draft_id}/metrics/{metric_id}"]["put"],
        paths["/api/v1/ontology/drafts/{draft_id}/validate"]["post"],
        paths["/api/v1/ontology/drafts/{draft_id}/publish"]["post"],
    ]
    for operation in operations:
        header = next(
            item for item in operation["parameters"] if item["name"] == "If-Match"
        )
        assert header["required"] is True
