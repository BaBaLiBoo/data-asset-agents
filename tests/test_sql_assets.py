import json
from pathlib import Path

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import QueryExecutionError
from data_asset_agents.ontology.models import PhysicalMapping
from data_asset_agents.ontology.retrieval import deterministic_embedding
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.models import (
    SQLAssetSearchRequest,
    SQLExecutionStatus,
)
from data_asset_agents.sql_assets.parser import HistoricalSQLParser
from data_asset_agents.sql_assets.repository import MemorySQLAssetRepository
from data_asset_agents.sql_assets.rewriter import SQLTemplateRewriter
from data_asset_agents.sql_assets.service import SQLAssetService
from data_asset_agents.text2sql.models import JoinPlan, QueryFilter, SemanticQuery, TimeRange


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
    assert report.eligible == 3
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
    parser = HistoricalSQLParser()
    assets = [parser.parse_asset(item, ontology) for item in _records()[:3]]
    for asset in assets:
        asset.execution_status = SQLExecutionStatus.EXPLAIN_PASSED
    service = SQLAssetService(
        MemorySQLAssetRepository(assets),
        ontology,
        ExplainExecutor(),
        Settings(embedding_dimensions=32),
    )
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
