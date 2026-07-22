from copy import deepcopy
from pathlib import Path

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
    CandidateObjectBinding,
    CandidateProperty,
    CreateDraftFromSeedRequest,
    CreateDraftRequest,
    DraftStatus,
    ImportObjectCandidatesRequest,
    LifecycleStatus,
    LinkType,
    MigrateLegacyRequest,
    ObjectCandidateSet,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    SemanticRole,
    TableRole,
)
from data_asset_agents.ontology.manager.projection import CompatibilityProjectionService
from data_asset_agents.ontology.manager.repository import MemoryOntologyManagerRepository
from data_asset_agents.ontology.manager.seed_repository import ObjectOntologySeedRepository
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.models import (
    CandidateConcept,
    CandidateEnvelope,
    ColumnMetadata,
    ColumnProfile,
    ColumnReference,
    ForeignKeyMetadata,
    HistoricalSQLAnalysis,
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


@pytest.fixture
def seed_repository():
    return ObjectOntologySeedRepository(
        {"retail_banking": Path("ontology/retail_banking/object_model")}
    )


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
        ],
    )
    generator = ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), bundle.tables)
    first = generator.generate(snapshot)
    second = generator.generate(snapshot)
    assert {item.object_type.id for item in first.object_types} >= {
        "customer",
        "account",
        "card",
        "transaction",
        "branch",
        "merchant",
    }
    assert all(
        item.table_role in {TableRole.CANONICAL_OBJECT, TableRole.EVENT}
        for item in first.object_types
    )
    assert any(item.property.id == "transaction.amount" for item in first.properties)
    assert any(
        item.binding.table_name == "dwd_card_transaction" for item in first.bindings
    )
    assert first.excluded_tables["dws_branch_transaction_day"] == "AGGREGATE_VIEW"
    assert first.excluded_tables["tmp_transaction_result"] == "TECHNICAL"
    assert first.excluded_tables["test_transaction_copy"] == "TECHNICAL"
    assert first.excluded_tables["legacy_card_transaction"] == "DEPRECATED"
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_property_candidate_contains_profile_and_historical_sql_evidence(
    bundle, seed_repository
) -> None:
    snapshot = MetadataSnapshot(
        id="snapshot-property-evidence",
        schema_name="public",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name="dwd_card_transaction",
                columns=[
                    ColumnMetadata(
                        name="transaction_id", data_type="BIGINT", nullable=False
                    ),
                    ColumnMetadata(name="posted_amount", data_type="NUMERIC", nullable=True),
                ],
                primary_key=["transaction_id"],
                profiles=[
                    ColumnProfile(
                        table_name="dwd_card_transaction",
                        column_name="posted_amount",
                        data_type="NUMERIC",
                        row_count=10,
                        null_count=1,
                        null_rate=0.1,
                        distinct_count=8,
                        unique_rate=0.8,
                        sample_values=["***12.00", "***18.50"],
                    )
                ],
            )
        ],
    )
    sql = HistoricalSQLAnalysis(
        id="certified-posted-amount",
        sql_text="SELECT SUM(posted_amount) FROM dwd_card_transaction",
        certified=True,
        tables=["dwd_card_transaction"],
        columns=[
            ColumnReference(table="dwd_card_transaction", column="posted_amount")
        ],
    )
    result = ObjectFirstCandidateGenerator(
        Settings(llm_mode="mock"), bundle.tables
    ).generate(snapshot, [sql], seed_repository.load("retail_banking"))
    candidate = next(
        item for item in result.properties if item.property.id == "transaction.posted_amount"
    )
    assert candidate.property.data_type == PropertyDataType.DECIMAL
    assert candidate.property.semantic_role == SemanticRole.MEASURE
    assert {item.source for item in candidate.evidence} >= {
        "metadata",
        "field-profile",
        "historical-sql",
    }


def test_metadata_fk_generates_separate_link_and_physical_join_candidates(bundle) -> None:
    snapshot = MetadataSnapshot(
        id="snapshot-fk",
        schema_name="public",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name="dim_customer",
                columns=[ColumnMetadata(name="customer_id", data_type="BIGINT", nullable=False)],
                primary_key=["customer_id"],
            ),
            TableMetadata(
                schema_name="public",
                table_name="dim_account",
                columns=[
                    ColumnMetadata(name="account_id", data_type="BIGINT", nullable=False),
                    ColumnMetadata(name="customer_id", data_type="BIGINT", nullable=False),
                ],
                primary_key=["account_id"],
                foreign_keys=[
                    ForeignKeyMetadata(
                        constrained_columns=["customer_id"],
                        referred_table="dim_customer",
                        referred_columns=["customer_id"],
                    )
                ],
            ),
        ],
    )
    result = ObjectFirstCandidateGenerator(
        Settings(llm_mode="mock"), bundle.tables
    ).generate(snapshot)
    assert result.link_types
    assert result.physical_joins
    assert result.link_types[0].link_type.physical_join_ids == [
        result.physical_joins[0].physical_join.id
    ]
    assert any(
        evidence.source == "foreign-key"
        for evidence in result.physical_joins[0].evidence
    )


def test_live_llm_output_cannot_choose_physical_binding_or_publish_state(bundle) -> None:
    class StructuredModel:
        @staticmethod
        def invoke(prompt):
            return {
                "object_name": "客户候选",
                "boundary_description": "仅供人工审核",
                "property_names": ["客户标识"],
                "property_roles": {"customer_id": "IDENTIFIER"},
                "confidence": 0.91,
                "evidence": ["masked metadata summary"],
                "table_name": "legacy_card_transaction",
                "column_bindings": {"customer.customer_id": "unsafe_column"},
                "lifecycle_status": "ACTIVE",
            }

    class ChatModel:
        @staticmethod
        def with_structured_output(model, **_kwargs):
            return StructuredModel()

    class Factory:
        @staticmethod
        def chat_model():
            return ChatModel()

    snapshot = MetadataSnapshot(
        id="snapshot-live-guardrail",
        schema_name="public",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name="dim_customer",
                columns=[
                    ColumnMetadata(
                        name="customer_id", data_type="BIGINT", nullable=False
                    )
                ],
                primary_key=["customer_id"],
            )
        ],
    )
    result = ObjectFirstCandidateGenerator(
        Settings(llm_mode="live", llm_api_key="test-placeholder"),
        bundle.tables,
        Factory(),  # type: ignore[arg-type]
    ).generate(snapshot)
    assert result.object_types[0].object_type.lifecycle_status == LifecycleStatus.DRAFT
    assert result.bindings[0].binding.table_name == "dim_customer"
    assert result.bindings[0].binding.property_bindings == {
        "customer.customer_id": "customer_id"
    }


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
    invalid.metrics[0].measure_property_id = "ghost.amount"
    invalid.dimensions[0].property_id = "ghost.name"
    report = validator.validate(invalid, "snapshot-one")
    codes = {item.code for item in report.issues}
    assert "OBJECT_PRIMARY_KEY_CONTRACT" in codes
    assert "PROPERTY_OBJECT_NOT_FOUND" in codes
    assert "BINDING_COLUMN_NOT_FOUND" in codes
    assert "LINK_PHYSICAL_JOIN_INVALID" in codes
    assert "METRIC_MEASURE_PROPERTY_INVALID" in codes
    assert "DIMENSION_PROPERTY_NOT_FOUND" in codes


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


def test_projection_preserves_analytical_ids_and_compiles_object_bindings(
    bundle, migrated
) -> None:
    projected = CompatibilityProjectionService().project(bundle, migrated)
    assert [item.id for item in projected.metrics] == [item.id for item in bundle.metrics]
    assert [item.id for item in projected.dimensions] == [
        item.id for item in bundle.dimensions
    ]
    metric = next(item for item in projected.metrics if item.id == "transaction_amount")
    assert metric.expression == "SUM(dwd_card_transaction.txn_amount_cny)"
    assert metric.required_filters == {"transaction_status": "POSTED"}
    branch = next(item for item in projected.dimensions if item.id == "branch")
    assert (branch.table, branch.column) == ("dim_branch", "branch_name")
    assert {item.concept_id for item in bundle.mappings} <= {
        item.concept_id for item in projected.mappings
    }
    assert len(projected.joins) >= len(bundle.joins)


def test_draft_state_machine_isolation_and_atomic_publication(
    bundle, seed_repository
) -> None:
    repository = MemoryOntologyManagerRepository()
    service = OntologyManagerService(
        repository,
        bundle,
        OntologyDraftValidator(bundle),
        seed_repository=seed_repository,
    )
    seeded = service.create_draft_from_seed(
        CreateDraftFromSeedRequest(draft_name="seeded", created_by="test")
    )
    assert len(seeded.resources.object_types) == 6
    assert service.published()[1].object_types == []

    validated = service.validate(seeded.draft.id)
    assert validated.draft.validation_report.valid
    reviewing = service.submit(seeded.draft.id, ActorRequest(actor="author"))
    assert reviewing.draft.status == DraftStatus.IN_REVIEW
    with pytest.raises(OntologyConflictError):
        service.save_resource(seeded.draft.id, seeded.resources.object_types[0])
    approved = service.approve(seeded.draft.id, ActorRequest(actor="reviewer"))
    assert approved.draft.status == DraftStatus.VALIDATED
    version = service.publish(
        seeded.draft.id,
        PublishDraftRequest(actor="reviewer", version="object-test-1"),
    )
    assert version.version == "object-test-1"
    assert len(service.published()[1].object_types) == 6
    with pytest.raises(OntologyConflictError):
        service.delete_draft(seeded.draft.id)


def test_seed_creation_never_calls_legacy_migrator(
    monkeypatch, bundle, seed_repository
) -> None:
    service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
        seed_repository=seed_repository,
    )
    monkeypatch.setattr(
        service.migrator,
        "migrate",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy path called")),
    )
    draft = service.create_draft_from_seed(CreateDraftFromSeedRequest())
    assert {item.id for item in draft.resources.object_types} >= {"transaction", "branch"}


def test_blank_draft_cannot_publish_legacy_analysis_by_fallback(bundle) -> None:
    service = OntologyManagerService(
        MemoryOntologyManagerRepository(), bundle, OntologyDraftValidator(bundle)
    )
    draft = service.create_draft(CreateDraftRequest(name="blank", created_by="test"))
    report = service.validate(draft.draft.id).draft.validation_report
    assert report is not None
    assert not report.valid
    assert {issue.code for issue in report.issues} >= {"OBJECT_TYPE_REQUIRED"}


def test_verified_field_candidate_imports_as_object_property(bundle, seed_repository) -> None:
    candidate = CandidateConcept(
        id="verified-posted-amount",
        snapshot_id="snapshot-one",
        table_name="dwd_card_transaction",
        column_name="posted_amount",
        semantic_id="attribute:transaction.posted_amount",
        business_name="入账金额候选",
        business_object="交易",
        semantic_property="仅供审核的虚构入账金额候选",
        role="measure",
        confidence=0.95,
        evidence=["reviewed test evidence"],
        status=ReviewStatus.VERIFIED,
    )

    class CandidateRepository:
        def get_metadata_snapshot(self, snapshot_id):
            assert snapshot_id == "snapshot-one"
            return MetadataSnapshot(
                id=snapshot_id,
                schema_name="public",
                tables=[
                    TableMetadata(
                        schema_name="public",
                        table_name="dwd_card_transaction",
                        columns=[
                            ColumnMetadata(
                                name="transaction_id", data_type="BIGINT", nullable=False
                            ),
                            ColumnMetadata(
                                name="posted_amount", data_type="NUMERIC", nullable=True
                            ),
                        ],
                        primary_key=["transaction_id"],
                    )
                ],
            )

        def list_historical_sql(self, snapshot_id):
            return []

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
        seed_repository=seed_repository,
        candidate_generator=ObjectFirstCandidateGenerator(
            Settings(llm_mode="mock"), bundle.tables
        ),
    )
    draft = service.create_draft_from_seed(
        CreateDraftFromSeedRequest(
            draft_name="candidate import",
            created_by="test",
            source_snapshot_id="snapshot-one",
        )
    )
    imported = service.import_candidates(
        draft.draft.id,
        ImportObjectCandidatesRequest(candidate_ids=[candidate.id], actor="reviewer"),
    )
    posted = next(
        item
        for item in imported.resources.properties
        if item.id == "transaction.posted_amount"
    )
    assert posted.object_type_id == "transaction"
    assert posted.semantic_role == SemanticRole.MEASURE


def test_candidate_import_does_not_overwrite_manual_property(
    bundle, seed_repository
) -> None:
    resources = seed_repository.load("retail_banking")
    manual = next(
        item for item in resources.properties if item.id == "transaction.status"
    ).model_copy(update={"description": "Manually reviewed status boundary"})
    proposed = manual.model_copy(update={"description": "Machine-generated replacement"})

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
            return ObjectCandidateSet(
                snapshot_id=snapshot.id,
                properties=[
                    CandidateProperty(
                        candidate_id="candidate-manual-status",
                        property=proposed,
                        confidence=0.9,
                    )
                ],
            )

    service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
        candidate_repository=CandidateRepository(),
        candidate_generator=Generator(),  # type: ignore[arg-type]
        seed_repository=seed_repository,
    )
    draft = service.create_draft_from_seed(
        CreateDraftFromSeedRequest(source_snapshot_id="snapshot-manual")
    )
    service.save_resource(draft.draft.id, manual)
    imported = service.import_candidates(
        draft.draft.id,
        ImportObjectCandidatesRequest(candidate_ids=["candidate-manual-status"]),
    )
    status = next(
        item
        for item in imported.resources.properties
        if item.id == "transaction.status"
    )
    assert status.description == "Manually reviewed status boundary"


def test_candidate_binding_conflict_is_explicit(bundle, seed_repository) -> None:
    resources = seed_repository.load("retail_banking")
    binding = next(
        item
        for item in resources.bindings
        if item.object_type_id == "transaction"
    ).model_copy(
        update={
            "property_bindings": {
                "transaction.amount": "posted_amount",
            }
        }
    )

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
            return ObjectCandidateSet(
                snapshot_id=snapshot.id,
                bindings=[
                    CandidateObjectBinding(
                        candidate_id="candidate-conflicting-binding",
                        binding=binding,
                        confidence=0.9,
                    )
                ],
            )

    service = OntologyManagerService(
        MemoryOntologyManagerRepository(),
        bundle,
        OntologyDraftValidator(bundle),
        candidate_repository=CandidateRepository(),
        candidate_generator=Generator(),  # type: ignore[arg-type]
        seed_repository=seed_repository,
    )
    draft = service.create_draft_from_seed(
        CreateDraftFromSeedRequest(source_snapshot_id="snapshot-conflict")
    )
    with pytest.raises(OntologyConflictError, match="conflicts"):
        service.import_candidates(
            draft.draft.id,
            ImportObjectCandidatesRequest(
                candidate_ids=["candidate-conflicting-binding"]
            ),
        )


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
    service.validate(draft.draft.id)
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
    service.validate(draft.draft.id)
    service.submit(draft.draft.id, ActorRequest())
    service.approve(draft.draft.id, ActorRequest())
    with pytest.raises(OntologyError):
        service.publish(draft.draft.id, PublishDraftRequest(version="failure-1"))
    assert service.published()[1].object_types == []
