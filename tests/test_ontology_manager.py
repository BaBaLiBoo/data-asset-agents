from copy import deepcopy

import pytest

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyConflictError, OntologyError
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.classifier import TableRoleClassifier
from data_asset_agents.ontology.manager.migration import LegacyOntologyObjectMigrator
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CreateDraftRequest,
    DraftStatus,
    LifecycleStatus,
    LinkType,
    MigrateLegacyRequest,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    SemanticRole,
    TableRole,
)
from data_asset_agents.ontology.manager.projection import CompatibilityProjectionService
from data_asset_agents.ontology.manager.repository import MemoryOntologyManagerRepository
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.models import (
    CandidateConcept,
    CandidateEnvelope,
    ColumnMetadata,
    MetadataSnapshot,
    ReviewStatus,
    TableMetadata,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository


@pytest.fixture
def bundle():
    return YamlOntologyRepository("ontology/retail_banking").load()


@pytest.fixture
def migrated(bundle):
    return LegacyOntologyObjectMigrator(bundle).migrate("snapshot-one")


def test_table_roles_and_aggregate_exclusion(bundle) -> None:
    classifier = TableRoleClassifier()
    roles = {table.name: classifier.classify(table)[0] for table in bundle.tables}
    assert roles["dim_customer"] == TableRole.CANONICAL_OBJECT
    assert roles["dwd_card_transaction"] == TableRole.EVENT
    assert roles["dws_branch_transaction_day"] == TableRole.AGGREGATE_VIEW
    assert roles["tmp_transaction_result"] == TableRole.TECHNICAL
    assert roles["test_transaction_copy"] == TableRole.TECHNICAL
    assert roles["legacy_card_transaction"] == TableRole.DEPRECATED
    resources = LegacyOntologyObjectMigrator(bundle).migrate()
    bound_tables = {item.table_name for item in resources.bindings}
    assert "dws_branch_transaction_day" not in bound_tables
    assert "tmp_transaction_result" not in bound_tables


def test_object_first_migration_is_stable_and_complete(bundle) -> None:
    migrator = LegacyOntologyObjectMigrator(bundle)
    first = migrator.migrate("snapshot-one")
    second = migrator.migrate("snapshot-one")
    assert [item.id for item in first.object_types] == [item.id for item in second.object_types]
    assert {item.id for item in first.object_types} == {
        "customer",
        "account",
        "card",
        "transaction",
        "branch",
        "merchant",
    }
    assert {item.id for item in first.link_types} >= {
        "customer_owns_account",
        "account_has_card",
        "card_generates_transaction",
        "transaction_belongs_to_branch",
        "transaction_occurs_at_merchant",
    }
    transaction = next(item for item in first.bindings if item.object_type_id == "transaction")
    assert transaction.property_bindings["transaction.amount"] == "txn_amount_cny"
    transaction_id = next(
        item for item in first.properties if item.id == "transaction.transaction_id"
    )
    assert transaction_id.data_type == PropertyDataType.INTEGER
    branch_id = next(item for item in first.properties if item.id == "transaction.branch_id")
    assert branch_id.semantic_role == SemanticRole.ATTRIBUTE


def test_mock_object_first_candidates_are_stable(bundle) -> None:
    snapshot = MetadataSnapshot(
        id="snapshot-object-candidates",
        schema_name="public",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name=table.name,
                columns=[
                    ColumnMetadata(name=column, data_type="VARCHAR", nullable=False)
                    for column in table.columns
                ],
                primary_key=[table.columns[0]],
            )
            for table in bundle.tables
            if table.status == "ACTIVE"
        ],
    )
    generator = ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), bundle)
    objects, properties, links, bindings = generator.generate(snapshot)
    assert {item.object_type.id for item in objects} == {
        "customer",
        "account",
        "card",
        "transaction",
        "branch",
        "merchant",
    }
    assert all(item.table_role in {TableRole.CANONICAL_OBJECT, TableRole.EVENT} for item in objects)
    assert any(item.property.id == "transaction.amount" for item in properties)
    assert any(item.link_type.id == "transaction_belongs_to_branch" for item in links)
    assert any(item.binding.table_name == "dwd_card_transaction" for item in bindings)


def test_draft_validator_covers_object_property_link_binding_and_lifecycle(
    bundle, migrated
) -> None:
    validator = OntologyDraftValidator(bundle)
    assert validator.validate(migrated, "snapshot-one").valid

    invalid = deepcopy(migrated)
    transaction = next(item for item in invalid.object_types if item.id == "transaction")
    transaction.primary_key_property_id = "transaction.status"
    invalid.properties.append(
        PropertyDefinition(
            id="ghost.value",
            object_type_id="ghost",
            name="Ghost",
            data_type=PropertyDataType.STRING,
            semantic_role=SemanticRole.ATTRIBUTE,
        )
    )
    invalid.bindings[0].property_bindings["account.account_id"] = "missing_column"
    invalid.link_types[0].physical_join_ids = ["missing_join"]
    report = validator.validate(invalid, "snapshot-one")
    codes = {item.code for item in report.issues}
    assert "OBJECT_PRIMARY_KEY_CONTRACT" in codes
    assert "PROPERTY_OBJECT_NOT_FOUND" in codes
    assert "BINDING_COLUMN_NOT_FOUND" in codes
    assert "LINK_PHYSICAL_JOIN_INVALID" in codes


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("legacy_card_transaction", "BINDING_TABLE_NOT_SELECTABLE"),
        ("tmp_transaction_result", "BINDING_TABLE_NOT_SELECTABLE"),
        ("test_transaction_copy", "BINDING_TABLE_NOT_SELECTABLE"),
    ],
)
def test_governed_bad_tables_cannot_bind_active_objects(bundle, migrated, table, expected) -> None:
    migrated.bindings[0].table_name = table
    migrated.bindings[0].primary_key_column = bundle.model_dump()["tables"][0]["columns"][0]
    report = OntologyDraftValidator(bundle).validate(migrated, "snapshot-one")
    assert expected in {item.code for item in report.issues}


def test_projection_preserves_all_analytical_resources(bundle, migrated) -> None:
    projected = CompatibilityProjectionService().project(bundle, migrated)
    assert [item.model_dump() for item in projected.metrics] == [
        item.model_dump() for item in bundle.metrics
    ]
    assert [item.model_dump() for item in projected.dimensions] == [
        item.model_dump() for item in bundle.dimensions
    ]
    assert {item.concept_id for item in bundle.mappings} <= {
        item.concept_id for item in projected.mappings
    }
    assert len(projected.joins) >= len(bundle.joins)


def test_draft_state_machine_isolation_and_atomic_publication(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    service = OntologyManagerService(
        repository,
        bundle,
        OntologyDraftValidator(bundle),
    )
    empty = service.create_draft(CreateDraftRequest(name="empty", created_by="test"))
    imported = service.import_candidates(empty.draft.id)
    assert len(imported.resources.object_types) == 6
    assert service.published()[1].object_types == []

    validated = service.validate(empty.draft.id)
    assert validated.draft.validation_report.valid
    reviewing = service.submit(empty.draft.id, ActorRequest(actor="author"))
    assert reviewing.draft.status == DraftStatus.IN_REVIEW
    with pytest.raises(OntologyConflictError):
        service.save_resource(empty.draft.id, imported.resources.object_types[0])
    approved = service.approve(empty.draft.id, ActorRequest(actor="reviewer"))
    assert approved.draft.status == DraftStatus.VALIDATED
    version = service.publish(
        empty.draft.id,
        PublishDraftRequest(actor="reviewer", version="object-test-1"),
    )
    assert version.version == "object-test-1"
    assert len(service.published()[1].object_types) == 6
    with pytest.raises(OntologyConflictError):
        service.delete_draft(empty.draft.id)


def test_verified_field_candidate_imports_as_object_property(bundle) -> None:
    candidate = CandidateConcept(
        id="verified-branch-region",
        snapshot_id="snapshot-one",
        table_name="dim_branch",
        column_name="region",
        semantic_id="attribute:branch.region",
        business_name="区域",
        business_object="分行",
        semantic_property="分行所属区域",
        role="dimension",
        confidence=0.95,
        evidence=["reviewed test evidence"],
        status=ReviewStatus.VERIFIED,
    )

    class CandidateRepository:
        def list_candidates(self, **kwargs):
            assert kwargs["status"] == ReviewStatus.VERIFIED
            return [
                CandidateEnvelope(
                    id=candidate.id,
                    candidate_type="concept",
                    status=ReviewStatus.VERIFIED,
                    payload=candidate.model_dump(mode="json"),
                )
            ]

    service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
        candidate_repository=CandidateRepository(),
    )
    draft = service.create_draft(
        CreateDraftRequest(
            name="candidate import",
            created_by="test",
            source_snapshot_id="snapshot-one",
        )
    )
    imported = service.import_candidates(draft.draft.id)
    region = next(item for item in imported.resources.properties if item.id == "branch.region")
    assert region.object_type_id == "branch"
    assert region.semantic_role == SemanticRole.DIMENSION


def test_rejected_and_unvalidated_drafts_cannot_publish(bundle) -> None:
    service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
    )
    draft = service.migrate_legacy(MigrateLegacyRequest(draft_name="reject me", created_by="test"))
    with pytest.raises(OntologyConflictError):
        service.publish(
            draft.draft.id,
            PublishDraftRequest(actor="test", version="invalid-1"),
        )
    service.submit(draft.draft.id, ActorRequest(actor="author"))
    rejected = service.reject(
        draft.draft.id,
        request=type("Request", (), {"actor": "reviewer", "reason": "duplicate"})(),
    )
    assert rejected.draft.status == DraftStatus.REJECTED
    with pytest.raises(OntologyConflictError):
        service.publish(
            draft.draft.id,
            PublishDraftRequest(actor="reviewer", version="invalid-2"),
        )


def test_invalid_link_object_is_reported(bundle, migrated) -> None:
    migrated.link_types.append(
        LinkType(
            id="ghost_link",
            name="Ghost",
            source_object_type_id="ghost",
            target_object_type_id="branch",
            source_role_name="branch",
            target_role_name="ghosts",
            cardinality="MANY_TO_ONE",
            physical_join_ids=["transaction_branch_join"],
            lifecycle_status=LifecycleStatus.ACTIVE,
        )
    )
    report = OntologyDraftValidator(bundle).validate(migrated, "snapshot-one")
    assert "LINK_OBJECT_NOT_FOUND" in {item.code for item in report.issues}


def test_publish_failure_does_not_expose_half_version(bundle, migrated) -> None:
    class FailingRepository(MemoryOntologyManagerRepository):
        def publish(self, *args, **kwargs):
            raise OntologyError("simulated transaction failure")

    repository = FailingRepository()
    service = OntologyManagerService(repository, bundle, OntologyDraftValidator(bundle))
    draft = service.migrate_legacy(MigrateLegacyRequest())
    service.submit(draft.draft.id, ActorRequest())
    service.approve(draft.draft.id, ActorRequest())
    with pytest.raises(OntologyError):
        service.publish(draft.draft.id, PublishDraftRequest(version="failure-1"))
    assert service.published()[1].object_types == []
