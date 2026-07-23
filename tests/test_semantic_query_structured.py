from pathlib import Path

from data_asset_agents.core.config import Settings
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.text2sql.models import SemanticQueryDraft, TimeRange
from data_asset_agents.text2sql.semantic_parser import SemanticQueryParser

ROOT = Path(__file__).parents[1]


class FakeStructuredModel:
    def __init__(self) -> None:
        self.schema = None

    def with_structured_output(self, schema, **_kwargs):
        self.schema = schema
        return self

    def invoke(self, _prompt: str):
        return SemanticQueryDraft(
            metrics=["credit_card_transaction_amount"],
            dimensions=["branch"],
            time_range=TimeRange(kind="relative_days", days=7),
            intent="aggregate",
            confidence=0.92,
        )


class FakeFactory:
    def __init__(self) -> None:
        self.model = FakeStructuredModel()

    def chat_model(self):
        return self.model


class StaticDraftFactory:
    def __init__(self, draft: SemanticQueryDraft) -> None:
        self.draft = draft

    def chat_model(self):
        draft = self.draft

        class StaticModel:
            def with_structured_output(self, _schema, **_kwargs):
                return self

            def invoke(self, _prompt: str):
                return draft

        return StaticModel()


def test_mock_semantic_query_supports_top_n_and_typed_ids() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(Settings(llm_mode="mock"))
    query = parser.parse("查询近30天各分行信用卡交易金额前5名", bundle)
    assert query.metric_ids == ["credit_card_transaction_amount"]
    assert query.dimension_ids == ["branch"]
    assert query.time_range.days == 30
    assert query.top_n == 5
    assert query.order_by[0].target == "credit_card_transaction_amount"
    assert not query.clarification_required


def test_mock_semantic_query_supports_business_filter_and_absolute_time() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(Settings(llm_mode="mock"))
    query = parser.parse(
        "查询2026-01-01到2026-01-31各分行交易金额，交易渠道为APP",
        bundle,
    )
    assert query.time_range.kind == "absolute"
    assert str(query.time_range.start) == "2026-01-01"
    assert query.filters[0].concept_id == "dimension:transaction_channel"
    assert query.filters[0].value == "APP"


def test_live_parser_uses_langchain_structured_output() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    factory = FakeFactory()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        factory,  # type: ignore[arg-type]
    )
    query = parser.parse("最近一周各分行信用卡交易金额", bundle)
    assert factory.model.schema is SemanticQueryDraft
    assert query.metric_ids == ["credit_card_transaction_amount"]
    assert query.dimension_ids == ["branch"]
    assert query.confidence == 0.92


def test_low_confidence_domain_question_requires_clarification() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(Settings(llm_mode="mock"))
    query = parser.parse("查询交易情况", bundle)
    assert query.clarification_required
    assert query.clarification_question


def test_high_confidence_missing_metric_still_clarifies_in_domain() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        StaticDraftFactory(
            SemanticQueryDraft(dimensions=["branch"], confidence=0.95)
        ),  # type: ignore[arg-type]
    )

    query = parser.parse("统计机构数量", bundle)

    assert query.clarification_required


def test_out_of_domain_unknown_candidate_is_not_misclassified_as_clarification() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        StaticDraftFactory(
            SemanticQueryDraft(metrics=["weather_temperature"], confidence=0.8)
        ),  # type: ignore[arg-type]
    )

    query = parser.parse("查询明天的天气", bundle)

    assert not query.clarification_required
    assert query.metric_ids == []


def test_filter_context_does_not_become_an_unrequested_grouping_dimension() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        StaticDraftFactory(
            SemanticQueryDraft(
                metrics=["credit_card_transaction_amount"],
                dimensions=["transaction_channel"],
                filters=[
                    {
                        "concept_id": "transaction_channel",
                        "operator": "IN",
                        "value": ["APP", "POS"],
                    }
                ],
                confidence=0.95,
            )
        ),  # type: ignore[arg-type]
    )

    filtered = parser.parse("查询APP和POS渠道信用卡交易金额", bundle)
    grouped = parser.parse("按渠道查询APP和POS信用卡交易金额", bundle)

    assert filtered.dimension_ids == []
    assert grouped.dimension_ids == ["transaction_channel"]


def test_vague_in_domain_request_cannot_bypass_clarification_with_model_guess() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        StaticDraftFactory(
            SemanticQueryDraft(metrics=["transaction_amount"], confidence=0.95)
        ),  # type: ignore[arg-type]
    )

    situation = parser.parse("查询交易情况", bundle)
    recent = parser.parse("看看最近的金额", bundle)

    assert situation.clarification_required
    assert recent.clarification_required


def test_metric_policy_removes_redundant_natural_language_filter() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    parser = SemanticQueryParser(
        Settings(llm_mode="live", llm_api_key="test-only"),
        StaticDraftFactory(
            SemanticQueryDraft(
                metrics=["credit_card_transaction_amount"],
                filters=[
                    {
                        "concept_id": "card_type",
                        "value": "贷记卡",
                    }
                ],
                confidence=0.95,
            )
        ),  # type: ignore[arg-type]
    )

    query = parser.parse("计算贷记卡消费金额", bundle)

    assert query.metric_ids == ["credit_card_transaction_amount"]
    assert query.filters == []
