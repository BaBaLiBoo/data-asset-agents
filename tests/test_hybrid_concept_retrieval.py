from pathlib import Path

from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.retrieval import HybridConceptRetriever

ROOT = Path(__file__).parents[1]


def test_exact_and_synonym_retrieval_return_evidence() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    retriever = HybridConceptRetriever()

    exact = retriever.search(bundle, "交易金额", 3)
    synonym = retriever.search(bundle, "消费金额", 5)

    assert exact[0].name == "交易金额"
    assert exact[0].exact_score == 1
    credit = next(item for item in synonym if item.name == "信用卡交易金额")
    assert credit.synonym_score == 1
    assert any("同义词" in evidence for evidence in credit.evidence)


def test_vector_component_recalls_semantically_related_concept() -> None:
    bundle = YamlOntologyRepository(ROOT / "ontology/retail_banking").load()
    result = HybridConceptRetriever().search(bundle, "人民币合计", 3)
    assert result[0].name == "交易金额"
    assert result[0].vector_score > 0
    assert result[0].exact_score == 0
    assert result[0].synonym_score == 0
