from data_asset_agents.ontology.service import OntologyService


def test_parse_mvp_question(ontology: OntologyService) -> None:
    query = ontology.parse("查询近30天各分行信用卡交易金额和交易笔数。")

    assert query.metric_names == ["信用卡交易金额", "信用卡交易笔数"]
    assert query.dimension_names == ["分行"]
    assert query.time_range.kind == "relative_days"
    assert query.time_range.days == 30


def test_synonyms_resolve_to_standard_concepts(ontology: OntologyService) -> None:
    query = ontology.parse("最近30天各机构消费金额和流水笔数")

    assert query.metric_names == ["信用卡交易金额", "信用卡交易笔数"]
    assert query.dimension_names == ["分行"]

