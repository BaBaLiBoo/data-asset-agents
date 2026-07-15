import os

import pytest

from data_asset_agents.core.config import Settings
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.graph import build_text2sql_graph

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 with an initialized PostgreSQL database",
)


def test_real_postgres_query_explains_and_returns_rows(
    ontology: OntologyService,
) -> None:
    settings = Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://minibank:minibank@localhost:5432/minibank",
        )
    )
    executor = QueryExecutor(settings, ontology.bundle)
    try:
        result = build_text2sql_graph(ontology, executor).invoke(
            {
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
                "retry_count": 0,
                "trace_steps": [],
            }
        )
    finally:
        executor.engine.dispose()

    assert result["status"] == "success"
    assert result["validation_report"].valid
    assert result["validation_report"].explain_passed
    assert result["execution_result"].row_count > 0
    assert result["execution_result"].explain_plan
    sql = result["generated_sql"]
    assert "legacy_card_transaction" not in sql
    assert "tmp_transaction_result" not in sql
    assert "test_transaction_copy" not in sql

