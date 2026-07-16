import inspect
import json
from pathlib import Path
from typing import Any

from data_asset_agents.core.config import Settings
from data_asset_agents.evaluation import (
    OntologyStrategy,
    PhysicalRAGIndex,
    PhysicalRAGStrategy,
    SchemaBaselineStrategy,
    StrategyRouter,
)
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.models import ExecutionResult
from data_asset_agents.validation import (
    CommonSQLSafetyValidator,
    DatabaseCatalog,
    EvaluationPolicyInspector,
    OntologyPolicyValidator,
)


class FakeExecutor:
    def execute(self, sql: str, allowed_tables: set[str]) -> ExecutionResult:
        return ExecutionResult(
            columns=["value"], rows=[{"value": 1}], row_count=1, explain_plan=["Mock"]
        )


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog.from_table_columns(
        {
            "dwd_card_transaction": {
                "transaction_id",
                "branch_id",
                "txn_amount",
                "txn_amount_cny",
                "card_type",
                "transaction_status",
                "transaction_date",
            },
            "dim_branch": {"branch_id", "branch_name"},
            "legacy_card_transaction": {"transaction_id", "txn_amount"},
        }
    )


def test_schema_and_rag_are_physically_isolated_from_ontology(tmp_path: Path) -> None:
    settings = Settings(llm_mode="mock", embedding_dimensions=32)
    schema = SchemaBaselineStrategy(_catalog(), FakeExecutor(), settings)
    assert "ontology" not in inspect.signature(SchemaBaselineStrategy).parameters
    assert not hasattr(schema, "ontology")
    schema_result = schema.execute("查询近30天各分行信用卡交易金额和交易笔数")
    assert schema_result.query_mode == "schema"
    assert schema_result.semantic_query is None
    assert schema_result.join_plan is None
    assert schema_result.selected_sql_asset is None
    assert schema_result.ontology_policy_report is None

    source = tmp_path / "history.json"
    source.write_text(
        json.dumps(
            [
                {
                    "question": "查询各分行交易金额",
                    "sql": (
                        "SELECT b.branch_name, SUM(t.txn_amount) "
                        "FROM dwd_card_transaction t JOIN dim_branch b "
                        "ON t.branch_id = b.branch_id GROUP BY b.branch_name"
                    ),
                    "certified": True,
                    "metrics": ["forbidden_metric_id"],
                    "ontology_version_id": "forbidden-version",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    index = PhysicalRAGIndex(32)
    index.build(_catalog(), source)
    serialized = " ".join(document.model_dump_json() for document in index.documents)
    assert "forbidden_metric_id" not in serialized
    assert "forbidden-version" not in serialized
    rag = PhysicalRAGStrategy(_catalog(), index, FakeExecutor(), settings)
    assert "ontology" not in inspect.signature(PhysicalRAGStrategy).parameters
    assert not hasattr(rag, "ontology")
    assert not hasattr(rag, "sql_assets")
    rag_result = rag.execute("查询各分行交易金额")
    assert rag_result.query_mode == "rag"
    assert rag_result.retrieved_context
    assert rag_result.semantic_query is None
    assert rag_result.selected_sql_asset is None
    assert rag_result.ontology_policy_report is None


def test_common_and_ontology_policy_are_separate(
    ontology: OntologyService,
) -> None:
    sql = "SELECT transaction_id FROM legacy_card_transaction"
    common = CommonSQLSafetyValidator(_catalog()).validate(sql)
    policy = OntologyPolicyValidator(ontology.bundle).validate(sql)
    assert common.valid
    assert "FORBIDDEN_LIFECYCLE_TABLE" not in {
        issue.code for issue in common.issues
    }
    assert not policy.valid
    assert "FORBIDDEN_LIFECYCLE_TABLE" in {issue.code for issue in policy.issues}


def test_evaluation_policy_inspector_never_modifies_sql(
    ontology: OntologyService,
) -> None:
    sql = "SELECT transaction_id FROM legacy_card_transaction"
    report = EvaluationPolicyInspector(ontology.bundle).inspect(sql)
    assert not report.valid
    assert sql == "SELECT transaction_id FROM legacy_card_transaction"
    assert not hasattr(report, "generated_sql")


class StubGraph:
    def __init__(self, include_asset: bool) -> None:
        self.include_asset = include_asset

    def invoke(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "generated_sql": "SELECT 1",
            "selected_tables": [],
            "selected_columns": {},
            "sql_asset_candidates": [StubModel({"id": "asset"})]
            if self.include_asset
            else [],
            "selected_sql_asset": StubModel({"id": "asset"})
            if self.include_asset
            else None,
            "sql_rewrite": StubModel({"used_template": True})
            if self.include_asset
            else None,
            "trace_steps": [],
        }


class StubModel:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.value


def test_four_strategy_variants_and_sql_asset_ablation() -> None:
    settings = Settings(llm_mode="mock", embedding_dimensions=32)
    schema = SchemaBaselineStrategy(_catalog(), FakeExecutor(), settings)
    source_index = PhysicalRAGIndex(32)
    source_index.documents = []
    rag = PhysicalRAGStrategy(_catalog(), source_index, FakeExecutor(), settings)
    no_asset = OntologyStrategy(StubGraph(False), sql_asset_enabled=False)
    full = OntologyStrategy(StubGraph(True), sql_asset_enabled=True)
    router = StrategyRouter(
        {
            "schema": schema,
            "rag": rag,
            "ontology_no_sql_asset": no_asset,
            "ontology_full": full,
        }
    )

    assert router.execute("交易金额", "schema").strategy_variant == "schema"
    assert router.execute("交易金额", "rag").strategy_variant == "rag"
    without = router.execute(
        "交易金额", "ontology", sql_asset_enabled=False
    )
    with_assets = router.execute("交易金额", "ontology", sql_asset_enabled=True)
    assert without.strategy_variant == "ontology_no_sql_asset"
    assert without.sql_asset_candidates is None
    assert without.selected_sql_asset is None
    assert without.sql_rewrite is None
    assert with_assets.strategy_variant == "ontology_full"
    assert with_assets.selected_sql_asset == {"id": "asset"}
