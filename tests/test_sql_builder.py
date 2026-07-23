from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.models import SemanticOrderBy
from data_asset_agents.text2sql.tools import JoinPlanner, build_select_sql


def test_sql_builder_uses_generic_aliases_and_reviewed_time_dimension(
    ontology: OntologyService,
) -> None:
    semantic = ontology.parse("查询近30天各分行信用卡交易金额和交易笔数。")
    metrics = ontology.get_metrics(semantic)
    dimensions = ontology.get_dimensions(semantic)
    resolved = ontology.resolve(semantic)
    plan = JoinPlanner(ontology.bundle.joins).plan(resolved["selected_tables"])

    semantic = semantic.model_copy(
        update={"order_by": [SemanticOrderBy(target="branch", direction="desc")]}
    )
    sql = build_select_sql(ontology, semantic, metrics, dimensions, plan)

    assert "FROM dwd_card_transaction t0" in sql
    assert "JOIN dim_branch t1 ON t0.branch_id = t1.branch_id" in sql
    assert "COUNT(DISTINCT t0.transaction_id)" in sql
    assert "t1.branch_name AS branch_name" in sql
    assert "ORDER BY branch_name DESC" in sql
    assert "t0.transaction_date >= DATE '2026-07-16'" in sql
    assert "dwd_card_transaction." not in sql


def test_object_model_uses_fact_card_type_and_reviewed_join_dimensions(
    ontology: OntologyService,
) -> None:
    card_type = ontology.dimensions_by_id["card_type"]
    transaction_count = ontology.metrics_by_id["transaction_count"]

    assert (card_type.table, card_type.column) == (
        "dwd_card_transaction",
        "card_type",
    )
    assert {"customer_type", "merchant_category"} <= set(
        transaction_count.supported_dimensions
    )
