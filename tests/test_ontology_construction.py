from pathlib import Path

import pytest
from pydantic import ValidationError

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.evaluation.ontology_construction import (
    GoldOntologyLoader,
    OntologyConstructionEvaluator,
)
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.construction import OntologyConstructionService
from data_asset_agents.ontology.manager.construction_repository import (
    MemoryConstructionRepository,
)
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CandidateLinkType,
    CandidateReviewDecision,
    CatalogMode,
    ConstructionCandidate,
    ConstructionCandidateStatus,
    ConstructionEvidenceMode,
    ConstructionMode,
    ConstructionRunStatus,
    CreateConstructionRunRequest,
    DraftResources,
    LinkCandidateLLMOutput,
    LinkSemanticSuggestion,
    MetricDefinition,
    ObjectCandidateLLMOutput,
    ObjectType,
    PromoteConstructionRunRequest,
    PropertyDataType,
    PropertyDefinition,
    PublishDraftRequest,
    ReviewConstructionCandidateRequest,
    SemanticRole,
    TableRole,
)
from data_asset_agents.ontology.manager.repository import (
    MemoryOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.models import (
    ColumnMetadata,
    ColumnProfile,
    ColumnReference,
    ForeignKeyMetadata,
    HistoricalSQLAnalysis,
    MetadataSnapshot,
    ParsedAggregate,
    TableMetadata,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.text2sql.semantic_parser import SemanticQueryParser
from scripts.run_ontology_construction_mock import _stage_provenance


def _snapshot() -> MetadataSnapshot:
    profiles = [
        ColumnProfile(
            table_name="dwd_card_transaction",
            column_name="transaction_status",
            data_type="VARCHAR",
            row_count=10,
            null_count=0,
            null_rate=0,
            distinct_count=2,
            unique_rate=0.2,
            sample_values=["POSTED", "PENDING"],
        )
    ]
    return MetadataSnapshot(
        id="snapshot-strict-construction",
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
                        name="txn_amount_cny", data_type="NUMERIC", nullable=False
                    ),
                    ColumnMetadata(
                        name="transaction_status", data_type="VARCHAR", nullable=False
                    ),
                    ColumnMetadata(
                        name="transaction_date", data_type="DATE", nullable=False
                    ),
                    ColumnMetadata(
                        name="card_type", data_type="VARCHAR", nullable=False
                    ),
                ],
                primary_key=["transaction_id"],
                profiles=profiles,
            ),
            TableMetadata(
                schema_name="public",
                table_name="dws_branch_transaction_day",
                columns=[
                    ColumnMetadata(
                        name="branch_id", data_type="BIGINT", nullable=False
                    )
                ],
                primary_key=["branch_id"],
            ),
            TableMetadata(
                schema_name="public",
                table_name="legacy_card_transaction",
                columns=[
                    ColumnMetadata(
                        name="transaction_id", data_type="BIGINT", nullable=False
                    )
                ],
                primary_key=["transaction_id"],
            ),
        ],
    )


def _sql() -> HistoricalSQLAnalysis:
    return HistoricalSQLAnalysis(
        id="sql-certified-amount",
        sql_text=(
            "SELECT SUM(txn_amount_cny) AS transaction_amount "
            "FROM dwd_card_transaction "
            "WHERE transaction_status = 'POSTED'"
        ),
        certified=True,
        certification_level="CERTIFIED",
        tables=["dwd_card_transaction"],
        columns=[
            ColumnReference(
                table="dwd_card_transaction", column="txn_amount_cny"
            ),
            ColumnReference(
                table="dwd_card_transaction", column="transaction_status"
            ),
        ],
        filters=["transaction_status = 'POSTED'"],
        aggregates=[
            ParsedAggregate(
                function="SUM",
                expression="SUM(txn_amount_cny)",
                alias="transaction_amount",
            )
        ],
    )


class EvidenceRepository:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.sql = [_sql()]

    def get_metadata_snapshot(self, snapshot_id: str) -> MetadataSnapshot:
        assert snapshot_id == self.snapshot.id
        return self.snapshot

    def list_historical_sql(
        self, snapshot_id: str
    ) -> list[HistoricalSQLAnalysis]:
        assert snapshot_id == self.snapshot.id
        return self.sql


def _promoted_construction_draft() -> tuple[
    OntologyManagerService, str, str
]:
    fallback = YamlOntologyRepository("ontology/retail_banking").load()
    run_repository = MemoryConstructionRepository()
    ontology_repository = MemoryOntologyManagerRepository()
    construction = OntologyConstructionService(
        run_repository,
        ontology_repository,
        EvidenceRepository(),
        ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), table_assets=[]),
    )
    run = construction.create_run(
        CreateConstructionRunRequest(
            source_snapshot_id=_snapshot().id,
            catalog_mode=CatalogMode.RAW_METADATA,
            construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
            evidence_mode=ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL,
        )
    )
    construction.generate(run.run_id)
    for candidate in construction.list_candidates(run.run_id):
        construction.review(
            run.run_id,
            candidate.candidate_id,
            ReviewConstructionCandidateRequest(
                decision=CandidateReviewDecision.ACCEPT,
                reviewer="test-reviewer",
            ),
        )
    aggregate = construction.promote_to_draft(
        run.run_id, PromoteConstructionRunRequest(actor="test-reviewer")
    )
    manager = OntologyManagerService(
        ontology_repository,
        fallback,
        OntologyDraftValidator(fallback),
    )
    return manager, aggregate.draft.id, run.run_id


def test_raw_metadata_generates_complete_review_candidates_without_gold() -> None:
    result = ObjectFirstCandidateGenerator(
        Settings(llm_mode="mock"), table_assets=[]
    ).generate(
        _snapshot(),
        [_sql()],
        catalog_mode=CatalogMode.RAW_METADATA,
    )
    assert result.object_types[0].table_role == TableRole.EVENT
    assert result.excluded_tables["dws_branch_transaction_day"] == "AGGREGATE_VIEW"
    assert result.excluded_tables["legacy_card_transaction"] == "DEPRECATED"
    assert any(
        item.property.id == "transaction.amount" for item in result.properties
    )
    assert any(
        item.dimension.property_id == "transaction.event_time"
        for item in result.dimensions
    )
    metric = next(item for item in result.metrics if item.metric.id == "transaction_amount")
    assert metric.metric.measure_property_id == "transaction.amount"
    assert metric.metric.aggregation == "SUM"
    assert metric.metric.filter_predicates[0].property_id == "transaction.status"
    assert all(item.evidence_hash for item in metric.evidence)


def test_strict_compiler_never_returns_legacy_metric_for_incomplete_draft() -> None:
    bundle = YamlOntologyRepository("ontology/retail_banking").load()
    seed = ObjectOntologySeedRepository(
        {"gold": Path("ontology/retail_banking/object_model")}
    ).load("gold")
    seed.metrics = []
    with pytest.raises(OntologyError, match="MetricDefinition"):
        ObjectSemanticCompiler(
            bundle, construction_mode=ConstructionMode.STRICT_CONSTRUCTION
        ).compile(seed)
    assert bundle.metrics


def test_construction_run_review_and_promote_preserves_stable_ids() -> None:
    run_repository = MemoryConstructionRepository()
    ontology_repository = MemoryOntologyManagerRepository()
    service = OntologyConstructionService(
        run_repository,
        ontology_repository,
        EvidenceRepository(),
        ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), table_assets=[]),
    )
    run = service.create_run(
        CreateConstructionRunRequest(
            source_snapshot_id=_snapshot().id,
            catalog_mode=CatalogMode.RAW_METADATA,
            construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
            evidence_mode=ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL,
        )
    )
    generated = service.generate(run.run_id)
    assert generated.status == ConstructionRunStatus.CANDIDATES_READY
    candidates = service.list_candidates(run.run_id)
    for candidate in candidates:
        service.review(
            run.run_id,
            candidate.candidate_id,
            ReviewConstructionCandidateRequest(
                decision=CandidateReviewDecision.ACCEPT,
                reviewer="test-reviewer",
            ),
        )
    aggregate = service.promote_to_draft(
        run.run_id, PromoteConstructionRunRequest(actor="test-reviewer")
    )
    assert aggregate.draft.construction_run_id == run.run_id
    assert aggregate.resources.metrics
    assert aggregate.resources.dimensions
    assert all(
        item.lifecycle_status == "ACTIVE"
        for item in (
            aggregate.resources.object_types
            + aggregate.resources.properties
            + aggregate.resources.metrics
            + aggregate.resources.dimensions
        )
    )


def test_modified_candidate_cannot_change_stable_id() -> None:
    service = OntologyConstructionService(
        MemoryConstructionRepository(),
        MemoryOntologyManagerRepository(),
        EvidenceRepository(),
        ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), table_assets=[]),
    )
    run = service.create_run(
        CreateConstructionRunRequest(source_snapshot_id=_snapshot().id)
    )
    service.generate(run.run_id)
    candidate = service.list_candidates(run.run_id, resource_type="property")[0]
    changed = {**candidate.current_resource, "id": "transaction.changed"}
    with pytest.raises(OntologyError, match="stable resource ID"):
        service.review(
            run.run_id,
            candidate.candidate_id,
            ReviewConstructionCandidateRequest(
                decision=CandidateReviewDecision.MODIFY,
                reviewer="test-reviewer",
                modified_resource=changed,
            ),
        )


def test_schema_only_ablation_removes_profile_and_sql_candidates() -> None:
    repository = MemoryConstructionRepository()
    service = OntologyConstructionService(
        repository,
        MemoryOntologyManagerRepository(),
        EvidenceRepository(),
        ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), table_assets=[]),
    )
    run = service.create_run(
        CreateConstructionRunRequest(
            source_snapshot_id=_snapshot().id,
            evidence_mode=ConstructionEvidenceMode.O_A_SCHEMA_ONLY,
        )
    )
    service.generate(run.run_id)
    candidates = service.list_candidates(run.run_id)
    assert not any(item.resource_type == "metric" for item in candidates)
    assert all(
        evidence.source_type not in {"FIELD_PROFILE", "CERTIFIED_HISTORICAL_SQL"}
        for item in candidates
        for evidence in item.evidence
    )


def test_certified_sql_keeps_distinct_metric_ids_for_the_same_measure() -> None:
    credit_sql = _sql().model_copy(
        update={
            "id": "sql-certified-credit-amount",
            "sql_text": (
                "SELECT SUM(txn_amount_cny) AS credit_card_transaction_amount "
                "FROM dwd_card_transaction "
                "WHERE transaction_status = 'POSTED' AND card_type = 'CREDIT'"
            ),
        }
    )
    result = ObjectFirstCandidateGenerator(
        Settings(llm_mode="mock"), table_assets=[]
    ).generate(
        _snapshot(),
        [_sql(), credit_sql],
        catalog_mode=CatalogMode.RAW_METADATA,
    )
    metric_ids = {item.metric.id for item in result.metrics}
    assert {"transaction_amount", "credit_card_transaction_amount"} <= metric_ids


def test_empty_strict_resources_fail_instead_of_returning_fallback() -> None:
    fallback = YamlOntologyRepository("ontology/retail_banking").load()
    with pytest.raises(OntologyError, match="ObjectType"):
        ObjectSemanticCompiler(
            fallback, construction_mode=ConstructionMode.STRICT_CONSTRUCTION
        ).compile(DraftResources())


def test_credit_context_recovers_count_without_legacy_transaction_count() -> None:
    resources = ObjectOntologySeedRepository(
        {"gold": Path("ontology/retail_banking/object_model")}
    ).load("gold")
    resources.metrics = [
        item for item in resources.metrics if item.id != "transaction_count"
    ]
    bundle = ObjectSemanticCompiler(
        None, construction_mode=ConstructionMode.STRICT_CONSTRUCTION
    ).compile(resources).bundle
    parsed = SemanticQueryParser(Settings(llm_mode="mock")).parse(
        "\u67e5\u8be2\u8fd130\u5929\u5404\u5206\u884c\u4fe1\u7528\u5361"
        "\u4ea4\u6613\u91d1\u989d\u548c\u4ea4\u6613\u7b14\u6570",
        bundle,
    )
    assert parsed.metric_ids == [
        "credit_card_transaction_amount",
        "credit_card_transaction_count",
    ]


def test_construction_draft_remains_strict_through_review_and_publish() -> None:
    manager, draft_id, run_id = _promoted_construction_draft()

    validated = manager.validate(draft_id)
    initial_report = validated.draft.validation_report
    assert initial_report is not None
    assert initial_report.valid
    assert initial_report.construction_mode == ConstructionMode.STRICT_CONSTRUCTION
    assert not initial_report.seed_accessed
    assert not initial_report.fallback_used
    assert not initial_report.legacy_ontology_accessed

    manager.submit(draft_id, ActorRequest(actor="author"))
    approved = manager.approve(draft_id, ActorRequest(actor="reviewer"))
    review_report = approved.draft.validation_report
    assert review_report is not None
    assert review_report.construction_mode == ConstructionMode.STRICT_CONSTRUCTION
    assert not review_report.seed_accessed
    assert not review_report.fallback_used
    assert not review_report.legacy_ontology_accessed

    version = manager.publish(
        draft_id,
        PublishDraftRequest(actor="reviewer", version="strict-construction-v1"),
    )
    artifact = manager.compiled_artifact(version.id)
    assert artifact is not None
    assert artifact.construction_run_id == run_id
    assert artifact.construction_mode == ConstructionMode.STRICT_CONSTRUCTION
    assert not artifact.seed_accessed
    assert not artifact.fallback_used
    assert not artifact.legacy_ontology_accessed


def test_construction_draft_missing_metric_cannot_use_fallback_or_publish() -> None:
    manager, draft_id, _ = _promoted_construction_draft()
    draft = manager.get_draft(draft_id)
    assert draft.resources.metrics
    manager.delete_resource(draft_id, "metric", draft.resources.metrics[0].id)

    failed = manager.validate(draft_id)
    report = failed.draft.validation_report
    assert report is not None
    assert not report.valid
    assert report.construction_mode == ConstructionMode.STRICT_CONSTRUCTION
    assert not report.seed_accessed
    assert not report.fallback_used
    assert not report.legacy_ontology_accessed
    assert any(
        "MetricDefinition" in issue.message
        for issue in report.issues
        if issue.code == "OBJECT_SEMANTIC_COMPILATION_FAILED"
    )
    with pytest.raises(OntologyError):
        manager.submit(draft_id, ActorRequest(actor="author"))
    with pytest.raises(OntologyError):
        manager.approve(draft_id, ActorRequest(actor="reviewer"))
    with pytest.raises(OntologyError):
        manager.publish(
            draft_id,
            PublishDraftRequest(actor="reviewer", version="must-not-publish"),
        )


def test_empty_comparisons_are_not_reported_as_perfect() -> None:
    evaluator = OntologyConstructionEvaluator()
    metrics, _ = evaluator._quality_metrics(DraftResources(), DraftResources())
    assert metrics["metric_precision"] is None
    assert metrics["metric_recall"] is None
    assert metrics["metric_f1"] is None
    assert metrics["metric_measure_property_accuracy"] is None
    assert metrics["link_endpoint_accuracy"] is None

    gold = DraftResources(
        metrics=[
            MetricDefinition(
                id="transaction_amount",
                name="Transaction amount",
                measure_property_id="transaction.amount",
                aggregation="SUM",
            )
        ]
    )
    missing, _ = evaluator._quality_metrics(DraftResources(), gold)
    assert missing["metric_recall"] == 0
    assert missing["metric_f1"] == 0
    assert missing["metric_measure_property_accuracy"] is None

    extra, _ = evaluator._quality_metrics(gold, DraftResources())
    assert extra["metric_precision"] == 0
    assert extra["metric_recall"] is None
    assert extra["metric_measure_property_accuracy"] is None


def test_raw_evaluation_is_frozen_before_gold_review_mutation() -> None:
    repository = MemoryConstructionRepository()
    service = OntologyConstructionService(
        repository,
        MemoryOntologyManagerRepository(),
        EvidenceRepository(),
        ObjectFirstCandidateGenerator(Settings(llm_mode="mock"), table_assets=[]),
        gold_loader=GoldOntologyLoader(Path("ontology/retail_banking/object_model")),
    )
    run = service.create_run(
        CreateConstructionRunRequest(source_snapshot_id=_snapshot().id)
    )
    service.generate(run.run_id)
    raw_before = service.evaluate_raw(run.run_id)
    candidate = service.list_candidates(run.run_id, resource_type="object_type")[0]
    service.review(
        run.run_id,
        candidate.candidate_id,
        ReviewConstructionCandidateRequest(
            decision=CandidateReviewDecision.MODIFY,
            reviewer="test-reviewer",
            modified_resource={**candidate.current_resource, "name": "Human name"},
        ),
    )
    raw_after = service.evaluate_raw(run.run_id)
    assert raw_after.raw_candidate_metrics == raw_before.raw_candidate_metrics
    persisted = repository.get_candidate(run.run_id, candidate.candidate_id)
    assert persisted is not None
    assert persisted.current_resource["name"] == "Human name"
    assert (
        persisted.original_candidate["object_type"]["name"]
        != persisted.current_resource["name"]
    )


def test_review_cost_classifies_fields_and_estimates_manual_baseline() -> None:
    prop = PropertyDefinition(
        id="transaction.amount",
        object_type_id="transaction",
        name="Amount",
        data_type=PropertyDataType.DECIMAL,
        semantic_role=SemanticRole.MEASURE,
    )
    candidate = ConstructionCandidate(
        candidate_id="candidate-property-1",
        run_id="run-1",
        resource_type="property",
        status=ConstructionCandidateStatus.MODIFIED,
        original_candidate={"property": prop.model_dump(mode="json")},
        current_resource={
            **prop.model_dump(mode="json"),
            "name": "Transaction amount",
            "semantic_role": "ATTRIBUTE",
        },
    )
    gold = DraftResources(
        object_types=[
            ObjectType(
                id="transaction",
                name="Transaction",
                plural_name="Transactions",
                property_ids=[prop.id],
            )
        ],
        properties=[prop],
    )
    cost = OntologyConstructionEvaluator()._review_cost([candidate], gold)
    assert cost["modified_candidate_count"] == 1
    assert cost["semantic_edit_count"] == 1
    assert cost["structural_edit_count"] == 1
    assert cost["total_edited_field_count"] == 2
    assert cost["manual_creation_field_fill_count"] > 2
    assert cost["review_saving_rate"] is not None


def test_foreign_key_business_link_has_stable_semantics_and_direction() -> None:
    snapshot = MetadataSnapshot(
        id="snapshot-link",
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
            ),
            TableMetadata(
                schema_name="public",
                table_name="dim_account",
                columns=[
                    ColumnMetadata(
                        name="account_id", data_type="BIGINT", nullable=False
                    ),
                    ColumnMetadata(
                        name="customer_id", data_type="BIGINT", nullable=False
                    ),
                    ColumnMetadata(
                        name="owner_customer_id", data_type="BIGINT", nullable=False
                    ),
                ],
                primary_key=["account_id"],
                foreign_keys=[
                    ForeignKeyMetadata(
                        constrained_columns=["customer_id"],
                        referred_table="dim_customer",
                        referred_columns=["customer_id"],
                    ),
                    ForeignKeyMetadata(
                        constrained_columns=["owner_customer_id"],
                        referred_table="dim_customer",
                        referred_columns=["customer_id"],
                    ),
                ],
            ),
        ],
    )
    generated = ObjectFirstCandidateGenerator(
        Settings(llm_mode="mock"), table_assets=[]
    ).generate(snapshot, catalog_mode=CatalogMode.RAW_METADATA)
    assert len(generated.physical_joins) == 2
    assert len(generated.link_types) == 1
    link = generated.link_types[0].link_type
    assert link.id == "customer_owns_account"
    assert link.source_object_type_id == "customer"
    assert link.target_object_type_id == "account"
    assert link.cardinality == "ONE_TO_MANY"
    assert link.target_role_name == "accounts"
    assert len(link.physical_join_ids) == 2


def test_link_endpoint_pair_and_direction_are_scored_separately() -> None:
    generated = ObjectFirstCandidateGenerator._business_link(
        "account",
        "customer",
        TableRole.CANONICAL_OBJECT,
        "account_customer_join",
        _snapshot().captured_at,
    )
    reversed_link = generated.model_copy(
        update={
            "source_object_type_id": generated.target_object_type_id,
            "target_object_type_id": generated.source_object_type_id,
        }
    )
    metrics, _ = OntologyConstructionEvaluator()._quality_metrics(
        DraftResources(link_types=[reversed_link]),
        DraftResources(link_types=[generated]),
    )
    assert metrics["link_endpoint_pair_f1"] == 1
    assert metrics["directed_link_f1"] == 0


def test_live_semantic_output_cannot_change_deterministic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = ObjectFirstCandidateGenerator(
        Settings(llm_mode="live", llm_api_key="test-only"), table_assets=[]
    )
    semantic = ObjectCandidateLLMOutput(
        object_name="Semantic transaction",
        boundary_description="A reviewed event boundary suggestion.",
        property_names=[
            "Identifier",
            "Amount",
            "Status",
            "Date",
            "Card type",
        ],
        property_roles={"txn_amount_cny": "ATTRIBUTE"},
        confidence=0.8,
    )
    monkeypatch.setattr(
        generator,
        "_live_output",
        lambda _object_id, _table: (
            semantic,
            {
                "provider": "test",
                "model": "structured-test",
                "invalid_output_count": 0,
            },
        ),
    )
    generated = generator.generate(
        _snapshot(), [_sql()], catalog_mode=CatalogMode.RAW_METADATA
    )
    amount = next(
        item.property
        for item in generated.properties
        if item.property.id == "transaction.amount"
    )
    binding = generated.bindings[0].binding
    assert amount.name == "Amount"
    assert amount.semantic_role == SemanticRole.MEASURE
    assert binding.table_name == "dwd_card_transaction"
    assert binding.property_bindings["transaction.amount"] == "txn_amount_cny"
    assert generated.llm_invocations[0]["invalid_output_count"] == 0


def test_live_structured_output_rejects_physical_coordinates() -> None:
    with pytest.raises(ValidationError):
        ObjectCandidateLLMOutput.model_validate(
            {
                "object_name": "Transaction",
                "boundary_description": "Event",
                "property_names": [],
                "confidence": 0.8,
                "table_name": "forbidden_physical_table",
            }
        )


def test_link_semantic_enrichment_preserves_structural_fields() -> None:
    link = ObjectFirstCandidateGenerator._business_link(
        "account",
        "customer",
        TableRole.CANONICAL_OBJECT,
        "account_customer_join",
        _snapshot().captured_at,
    )
    wrapped = CandidateLinkType(
        candidate_id="candidate-link-1",
        link_type=link,
        confidence=0.9,
    )
    output = LinkCandidateLLMOutput(
        suggestions=[
            LinkSemanticSuggestion(
                link_id=link.id,
                business_name="Customer owns account",
                inverse_name="accounts",
            )
        ]
    )
    enriched = ObjectFirstCandidateGenerator._apply_link_enrichment(
        [wrapped], output
    )[0].link_type
    assert enriched.name == "Customer owns account"
    assert enriched.target_role_name == "accounts"
    assert enriched.id == link.id
    assert enriched.source_object_type_id == link.source_object_type_id
    assert enriched.target_object_type_id == link.target_object_type_id
    assert enriched.cardinality == link.cardinality
    assert enriched.physical_join_ids == link.physical_join_ids


@pytest.mark.parametrize(
    ("field", "expected_stage"),
    [
        ("raw_candidate_metrics", "RAW_CANDIDATE"),
        ("reviewed_draft_metrics", "REVIEWED_DRAFT"),
        ("review_cost", "REVIEW_PROCESS"),
        ("review_delta", "REVIEW_DELTA"),
        ("error_analysis", "RAW_AND_REVIEWED"),
    ],
)
def test_split_report_provenance_uses_its_actual_stage(
    field: str, expected_stage: str
) -> None:
    original = {
        "evaluation_stage": "REVIEWED_DRAFT",
        "git_sha": "a" * 40,
    }
    staged = _stage_provenance({"provenance": original}, field)
    assert staged["evaluation_stage"] == expected_stage
    assert staged["git_sha"] == "a" * 40
    assert original["evaluation_stage"] == "REVIEWED_DRAFT"
