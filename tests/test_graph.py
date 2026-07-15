from decimal import Decimal

from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.graph import build_text2sql_graph
from data_asset_agents.text2sql.models import ExecutionResult


class FakeExecutor:
    def execute(self, sql: str, allowed_tables: set[str]) -> ExecutionResult:
        assert allowed_tables == {"dwd_card_transaction", "dim_branch"}
        assert "COUNT(DISTINCT t.transaction_id)" in sql
        return ExecutionResult(
            columns=["branch", "amount", "count"],
            rows=[{"branch": "MiniBank示例分行1", "amount": Decimal("1288.50"), "count": 3}],
            row_count=1,
            explain_plan=["GroupAggregate"],
        )


def test_mvp_graph_end_to_end(ontology: OntologyService) -> None:
    graph = build_text2sql_graph(ontology, FakeExecutor())
    result = graph.invoke(
        {
            "question": "查询近30天各分行信用卡交易金额和交易笔数。",
            "query_mode": "ontology",
            "retry_count": 0,
            "trace_steps": [],
        }
    )

    assert result["metrics"] == ["信用卡交易金额", "信用卡交易笔数"]
    assert result["selected_tables"] == ["dwd_card_transaction", "dim_branch"]
    assert result["validation_report"].valid
    assert result["execution_result"].row_count == 1
    assert result["confidence"] > 0.9

