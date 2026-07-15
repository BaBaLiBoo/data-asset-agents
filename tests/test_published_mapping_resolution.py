from pathlib import Path

from data_asset_agents.ontology.models import PhysicalMapping
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService

ROOT = Path(__file__).parents[1]


class StubPublishedRepository:
    def load_latest_published_bundle(self):
        seed = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
        mappings = [
            mapping
            for mapping in seed.mappings
            if mapping.concept_id != "metric:credit_card_transaction_amount"
        ]
        mappings.append(
            PhysicalMapping(
                concept_id="metric:credit_card_transaction_amount",
                table="dwd_card_transaction",
                column_bindings={
                    "amount": "posted_amount",
                    "card_type": "card_type",
                    "status": "transaction_status",
                    "event_time": "transaction_date",
                },
            )
        )
        domain = dict(seed.domain)
        domain["version"] = "published-test"
        return seed.model_copy(update={"domain": domain, "mappings": mappings})


def test_online_metric_uses_published_physical_mapping_instead_of_base_table() -> None:
    yaml_repository = YamlOntologyRepository(ROOT / "ontology/retail_banking")
    service = OntologyService(yaml_repository, StubPublishedRepository())  # type: ignore[arg-type]
    semantic = service.parse("查询信用卡交易金额")

    metric = service.get_metrics(semantic)[0]

    assert service.bundle.domain["version"] == "published-test"
    assert metric.base_table == "dwd_card_transaction"
    assert metric.expression == "SUM(dwd_card_transaction.posted_amount)"
