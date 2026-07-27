from pathlib import Path

import pytest

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.construction import OntologyConstructionService
from data_asset_agents.ontology.manager.construction_repository import (
    MemoryConstructionRepository,
)
from data_asset_agents.ontology.manager.models import (
    CandidateReviewDecision,
    CatalogMode,
    ConstructionEvidenceMode,
    ConstructionMode,
    ConstructionRunStatus,
    CreateConstructionRunRequest,
    DraftResources,
    PromoteConstructionRunRequest,
    ReviewConstructionCandidateRequest,
    TableRole,
)
from data_asset_agents.ontology.manager.repository import (
    MemoryOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)
from data_asset_agents.ontology.models import (
    ColumnMetadata,
    ColumnProfile,
    ColumnReference,
    HistoricalSQLAnalysis,
    MetadataSnapshot,
    ParsedAggregate,
    TableMetadata,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository


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


def test_empty_strict_resources_fail_instead_of_returning_fallback() -> None:
    fallback = YamlOntologyRepository("ontology/retail_banking").load()
    with pytest.raises(OntologyError, match="ObjectType"):
        ObjectSemanticCompiler(
            fallback, construction_mode=ConstructionMode.STRICT_CONSTRUCTION
        ).compile(DraftResources())
