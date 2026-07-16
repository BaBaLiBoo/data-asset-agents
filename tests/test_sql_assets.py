import json
from pathlib import Path

import pytest

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import QueryExecutionError
from data_asset_agents.ontology.models import PhysicalMapping
from data_asset_agents.ontology.retrieval import deterministic_embedding
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.models import (
    SQLAssetBuild,
    SQLAssetBuildStatus,
    SQLAssetScore,
    SQLAssetSearchRequest,
    SQLAssetSearchResult,
    SQLExecutionStatus,
)
from data_asset_agents.sql_assets.parser import HistoricalSQLParser
from data_asset_agents.sql_assets.repository import MemorySQLAssetRepository
from data_asset_agents.sql_assets.rewriter import SQLTemplateRewriter
from data_asset_agents.sql_assets.service import SQLAssetService
from data_asset_agents.text2sql.models import JoinPlan, QueryFilter, SemanticQuery, TimeRange
from data_asset_agents.text2sql.nodes import Text2SQLNodes


class ExplainExecutor:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def explain(self, sql: str, allowed_tables: set[str]) -> list[str]:
        if self.fail:
            raise QueryExecutionError("synthetic EXPLAIN failure")
        assert sql.upper().startswith(("SELECT", "WITH"))
        assert allowed_tables
        return ["Aggregate"]


def _records() -> list[dict[str, object]]:
    return json.loads(Path("data/historical_sql/examples.json").read_text(encoding="utf-8"))


def test_sql_asset_parser_extracts_cte_window_and_stable_fingerprint(
    ontology: OntologyService,
) -> None:
    parser = HistoricalSQLParser()
    complex_asset = parser.parse_asset(_records()[2], ontology)
    assert complex_asset.ctes == ["branch_totals"]
    assert complex_asset.window_functions
    assert {"cte", "window", "aggregate"} <= set(complex_asset.structural_tags)
    assert complex_asset.invalid_columns == []

    first = parser.parse_asset(
        {
            "question": "q",
            "sql": "SELECT COUNT(*) FROM dwd_card_transaction WHERE card_type = 'CREDIT'",
        },
        ontology,
    )
    second = parser.parse_asset(
        {
            "question": "q",
            "sql": " SELECT COUNT(*)  FROM dwd_card_transaction WHERE card_type='DEBIT' ",
        },
        ontology,
    )
    assert first.ast_fingerprint == second.ast_fingerprint
    assert first.ast_node_types["Count"] == 1
    subquery = parser.parse_asset(
        {
            "question": "子查询示例",
            "sql": (
                "SELECT transaction_id FROM dwd_card_transaction "
                "WHERE txn_amount_cny > (SELECT AVG(txn_amount_cny) "
                "FROM dwd_card_transaction)"
            ),
        },
        ontology,
    )
    assert subquery.subquery_count == 1
    assert "subquery" in subquery.structural_tags


def test_build_indexes_mock_vectors_and_hard_excludes_invalid_assets(
    ontology: OntologyService,
) -> None:
    repository = MemorySQLAssetRepository()
    service = SQLAssetService(
        repository,
        ontology,
        ExplainExecutor(),
        Settings(llm_mode="mock", embedding_dimensions=64),
    )
    report = service.build()
    assert report.indexed == len(_records())
    assert report.eligible == 11
    assert report.build.status == SQLAssetBuildStatus.READY
    assert report.build.ontology_version_id == ontology.ontology_version_id
    assert all(asset.build_id == report.build.build_id for asset in report.assets)
    assert all(asset.source_hash == report.build.source_hash for asset in report.assets)
    assert repository.get("sqlasset-deprecated-example").lifecycle_valid is False  # type: ignore[union-attr]
    assert repository.get("sqlasset-unapproved-join").unapproved_joins  # type: ignore[union-attr]

    semantic = ontology.parse("查询近30天各分行信用卡交易金额")
    resolved = ontology.resolve(semantic)
    results = service.search(
        SQLAssetSearchRequest(
            question="查询近30天各分行信用卡交易金额",
            semantic_query=semantic,
            selected_tables=resolved["selected_tables"],
            selected_columns=resolved["selected_columns"],
            join_conditions=[
                "dwd_card_transaction.branch_id = dim_branch.branch_id"
            ],
        )
    )
    ids = {item.asset.id for item in results}
    assert "sqlasset-deprecated-example" not in ids
    assert "sqlasset-unapproved-join" not in ids
    assert "sqlasset-uncertified-example" not in ids
    assert results[0].score.total >= results[-1].score.total
    assert results[0].score.metric_match == 1
    assert results[0].evidence


def test_deterministic_embedding_and_semantic_structure_reranking(
    ontology: OntologyService,
) -> None:
    assert deterministic_embedding("认证 SQL", 32) == deterministic_embedding("认证 SQL", 32)
    repository = MemorySQLAssetRepository()
    service = SQLAssetService(
        repository,
        ontology,
        ExplainExecutor(),
        Settings(embedding_dimensions=32),
    )
    service.build()
    semantic = ontology.parse("查询各分行信用卡交易金额")
    results = service.search(
        SQLAssetSearchRequest(
            question="查询各分行信用卡交易金额",
            semantic_query=semantic,
            selected_tables=["dwd_card_transaction", "dim_branch"],
            selected_columns={
                "dwd_card_transaction": ["txn_amount_cny"],
                "dim_branch": ["branch_name"],
            },
            join_conditions=[
                "dwd_card_transaction.branch_id = dim_branch.branch_id"
            ],
        )
    )
    assert results[0].asset.id == "sqlasset-branch-credit-amount"
    assert results[0].score.table_column_coverage == 1
    assert results[0].score.join_match == 1


def test_metric_policy_hard_gates_cover_required_business_rules(
    ontology: OntologyService,
) -> None:
    assets = {
        asset.id: asset
        for asset in HistoricalSQLParser().parse_assets(
            "data/historical_sql/examples.json", ontology
        )
    }
    expected = {
        "sqlasset-missing-posted": "MISSING_REQUIRED_FILTER:transaction_status=POSTED",
        "sqlasset-missing-credit": "MISSING_REQUIRED_FILTER:card_type=CREDIT",
        "sqlasset-wrong-time-field": "TIME_FIELD_MISMATCH",
        "sqlasset-unsupported-dimension": "UNSUPPORTED_DIMENSION:customer_type",
        "sqlasset-conflicting-filter": "CONFLICTING_FILTER",
    }
    for asset_id, violation in expected.items():
        asset = assets[asset_id]
        assert not asset.semantic_policy_valid
        assert any(violation in item for item in asset.metric_policy_violations)
        assert not asset.hard_eligible
    wrong_role = HistoricalSQLParser().parse_asset(
        {
            "question": "错误金额角色",
            "sql": (
                "SELECT SUM(t.posted_amount) FROM dwd_card_transaction t "
                "WHERE t.card_type = 'CREDIT' "
                "AND t.transaction_status = 'POSTED'"
            ),
            "certified": True,
            "metrics": ["credit_card_transaction_amount"],
            "dimensions": [],
        },
        ontology,
    )
    assert not wrong_role.semantic_policy_valid
    assert any(
        "AGGREGATION_OR_ROLE_MISMATCH" in item
        for item in wrong_role.metric_policy_violations
    )


def test_failed_build_keeps_previous_ready_index(
    ontology: OntologyService,
) -> None:
    repository = MemorySQLAssetRepository()
    stable = SQLAssetService(
        repository,
        ontology,
        ExplainExecutor(),
        Settings(embedding_dimensions=32),
    )
    first = stable.build().build

    class FailingEmbeddingService(SQLAssetService):
        def _embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("synthetic embedding outage")

    failing = FailingEmbeddingService(
        repository,
        ontology,
        ExplainExecutor(),
        Settings(embedding_dimensions=32),
    )
    with pytest.raises(Exception, match="synthetic embedding outage"):
        failing.build()
    assert repository.latest_ready(ontology.ontology_version_id) == first
    assert any(
        build.status == SQLAssetBuildStatus.FAILED
        for build in repository.builds.values()
    )


def test_building_index_is_invisible_and_live_startup_does_not_build(
    ontology: OntologyService,
) -> None:
    repository = MemorySQLAssetRepository()
    service = SQLAssetService(
        repository,
        ontology,
        ExplainExecutor(),
        Settings(embedding_dimensions=32),
    )
    ready = service.build().build
    building = SQLAssetBuild(
        build_id="sqlbuild-in-progress",
        ontology_version_id=ontology.ontology_version_id,
        source_hash="1" * 64,
        source_path="data/historical_sql/examples.json",
    )
    repository.create_build(building)
    old_asset = repository.get("sqlasset-branch-credit-amount")
    assert old_asset is not None
    repository.upsert(
        old_asset.model_copy(update={"build_id": building.build_id}),
        deterministic_embedding(old_asset.retrieval_text, 32),
    )
    semantic = ontology.parse("查询各分行信用卡交易金额")
    results = service.search(
        SQLAssetSearchRequest(
            question="查询各分行信用卡交易金额",
            semantic_query=semantic,
            selected_tables=["dwd_card_transaction", "dim_branch"],
            selected_columns={},
            join_conditions=[
                "dwd_card_transaction.branch_id = dim_branch.branch_id"
            ],
        )
    )
    assert results
    assert all(item.asset.build_id == ready.build_id for item in results)

    empty_repository = MemorySQLAssetRepository()
    live = SQLAssetService(
        empty_repository,
        ontology,
        ExplainExecutor(),
        Settings(llm_mode="live", embedding_dimensions=32),
    )
    assert live.initialize_if_needed() is None
    assert not empty_repository.builds


def test_top_k_checker_skips_incompatible_template(
    ontology: OntologyService,
) -> None:
    parser = HistoricalSQLParser()
    incompatible = parser.parse_asset(_records()[0], ontology)
    compatible = parser.parse_asset(_records()[2], ontology)
    for asset in (incompatible, compatible):
        asset.ontology_version_id = ontology.ontology_version_id
        asset.execution_status = SQLExecutionStatus.EXPLAIN_PASSED
    results = [
        SQLAssetSearchResult(asset=incompatible, score=SQLAssetScore(total=0.99)),
        SQLAssetSearchResult(asset=compatible, score=SQLAssetScore(total=0.95)),
    ]

    class StubSearch:
        def search(self, request: SQLAssetSearchRequest) -> list[SQLAssetSearchResult]:
            return results

    nodes = Text2SQLNodes(ontology, ExplainExecutor(), sql_assets=StubSearch())  # type: ignore[arg-type]
    semantic = ontology.parse("查询近30天各分行信用卡交易金额和排名")
    resolved = ontology.resolve(semantic)
    plan = JoinPlan.model_validate(
        {
            "tables": ["dwd_card_transaction", "dim_branch"],
            "steps": [
                {
                    "left_table": "dwd_card_transaction",
                    "right_table": "dim_branch",
                    "left_column": "branch_id",
                    "right_column": "branch_id",
                    "condition": "dwd_card_transaction.branch_id = dim_branch.branch_id",
                    "relationship": "many_to_one",
                }
            ],
        }
    )
    selected_columns = resolved["selected_columns"]
    selected_columns["dwd_card_transaction"].append("branch_id")
    selected_columns["dim_branch"].append("branch_id")
    output = nodes.retrieve_historical_sql(
        {
            "question": "查询近30天各分行信用卡交易金额和排名",
            "semantic_query": semantic,
            "selected_tables": plan.tables,
            "selected_columns": selected_columns,
            "join_plan": plan,
        }
    )
    assert output["selected_sql_asset"].id == compatible.id
    assert output["selected_template_rank"] == 2
    assert incompatible.id in output["template_rejection_reasons"]


def test_ast_rewriter_replaces_table_column_time_filter_order_and_limit(
    ontology: OntologyService,
) -> None:
    parser = HistoricalSQLParser()
    asset = parser.parse_asset(_records()[1], ontology)
    target_mapping = PhysicalMapping(
        concept_id="metric:credit_card_transaction_count",
        table="dwd_account_transaction",
        column_bindings={
            "transaction_id": "transaction_id",
            "card_type": "account_type",
            "status": "transaction_status",
            "event_time": "transaction_date",
        },
    )
    deterministic = (
        "SELECT COUNT(DISTINCT t0.transaction_id) AS credit_card_transaction_count "
        "FROM dwd_account_transaction t0 WHERE t0.account_type = 'CREDIT' "
        "AND t0.transaction_status = 'POSTED' "
        "AND t0.transaction_date >= CURRENT_DATE - INTERVAL '7 days' "
        "ORDER BY credit_card_transaction_count DESC LIMIT 5"
    )
    rewritten, changes = SQLTemplateRewriter(
        ontology, ExplainExecutor()
    ).rewrite_ast(
        asset,
        deterministic,
        SemanticQuery(
            metric_ids=["credit_card_transaction_count"],
            time_range=TimeRange(kind="relative_days", days=7),
            limit=5,
        ),
        [target_mapping],
        JoinPlan(tables=["dwd_account_transaction"]),
    )
    assert "dwd_account_transaction" in rewritten
    assert "account_type = 'CREDIT'" in rewritten
    assert "INTERVAL '7 DAYS'" in rewritten.upper()
    assert "LIMIT 5" in rewritten
    assert changes


def test_ast_rewrite_preserves_complex_structure_and_falls_back_on_explain(
    ontology: OntologyService,
) -> None:
    asset = HistoricalSQLParser().parse_asset(_records()[2], ontology)
    semantic = ontology.parse("查询近30天各分行信用卡交易金额")
    mappings = [
        mapping
        for mapping in ontology.bundle.mappings
        if mapping.concept_id
        in {"metric:credit_card_transaction_amount", "dimension:branch"}
    ]
    deterministic = (
        "SELECT t1.branch_name AS branch, SUM(t0.txn_amount_cny) "
        "AS credit_card_transaction_amount FROM dwd_card_transaction t0 "
        "JOIN dim_branch t1 ON t0.branch_id = t1.branch_id "
        "WHERE t0.card_type = 'CREDIT' AND t0.transaction_status = 'POSTED' "
        "AND t0.transaction_date >= CURRENT_DATE - INTERVAL '30 days' "
        "GROUP BY t1.branch_name ORDER BY t1.branch_name"
    )
    plan = JoinPlan.model_validate(
        {
            "tables": ["dwd_card_transaction", "dim_branch"],
            "steps": [
                {
                    "left_table": "dwd_card_transaction",
                    "right_table": "dim_branch",
                    "left_column": "branch_id",
                    "right_column": "branch_id",
                    "condition": "dwd_card_transaction.branch_id = dim_branch.branch_id",
                    "relationship": "many_to_one",
                }
            ],
        }
    )
    rewriter = SQLTemplateRewriter(ontology, ExplainExecutor(fail=True))
    rewritten, _ = rewriter.rewrite_ast(asset, deterministic, semantic, mappings, plan)
    assert "WITH branch_totals" in rewritten
    assert "DENSE_RANK() OVER" in rewritten

    successful = SQLTemplateRewriter(ontology, ExplainExecutor()).rewrite_or_fallback(
        asset,
        deterministic,
        semantic,
        mappings,
        plan,
        [
            QueryFilter(
                table="dwd_card_transaction",
                field="card_type",
                value="CREDIT",
                source="metric_policy",
            ),
            QueryFilter(
                table="dwd_card_transaction",
                field="transaction_status",
                value="POSTED",
                source="metric_policy",
            ),
        ],
    )
    assert successful.used_template
    assert "WITH branch_totals" in str(successful.rewritten_sql)

    result = rewriter.rewrite_or_fallback(
        asset,
        deterministic,
        semantic,
        mappings,
        plan,
        [
            QueryFilter(
                table="dwd_card_transaction",
                field="card_type",
                value="CREDIT",
                source="metric_policy",
            ),
            QueryFilter(
                table="dwd_card_transaction",
                field="transaction_status",
                value="POSTED",
                source="metric_policy",
            ),
        ],
    )
    assert result.used_template is False
    assert result.rewritten_sql == deterministic
    assert "确定性编译器" in str(result.fallback_reason)


def test_complex_template_with_different_join_cannot_be_reused(
    ontology: OntologyService,
) -> None:
    asset = HistoricalSQLParser().parse_asset(_records()[2], ontology)
    semantic = ontology.parse("查询近30天各分行信用卡交易金额和排名")
    mappings = [
        mapping
        for mapping in ontology.bundle.mappings
        if mapping.concept_id
        in {"metric:credit_card_transaction_amount", "dimension:branch"}
    ]
    incompatible_plan = JoinPlan.model_validate(
        {
            "tables": ["dwd_card_transaction", "dim_branch"],
            "steps": [
                {
                    "left_table": "dwd_card_transaction",
                    "right_table": "dim_branch",
                    "left_column": "customer_id",
                    "right_column": "branch_id",
                    "condition": "dwd_card_transaction.customer_id = dim_branch.branch_id",
                    "relationship": "many_to_one",
                }
            ],
        }
    )
    deterministic = (
        "SELECT t1.branch_name, SUM(t0.txn_amount_cny) "
        "FROM dwd_card_transaction t0 JOIN dim_branch t1 "
        "ON t0.customer_id = t1.branch_id WHERE t0.card_type = 'CREDIT' "
        "AND t0.transaction_status = 'POSTED' GROUP BY t1.branch_name"
    )
    result = SQLTemplateRewriter(ontology, ExplainExecutor()).rewrite_or_fallback(
        asset,
        deterministic,
        semantic,
        mappings,
        incompatible_plan,
        [],
    )
    assert not result.used_template
    assert "Join" in str(result.fallback_reason)
