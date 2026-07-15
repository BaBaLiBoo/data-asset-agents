from data_asset_agents.ontology.service import OntologyService


def test_resolve_selects_certified_detail_assets(ontology: OntologyService) -> None:
    semantic = ontology.parse("查询近30天各分行信用卡交易金额和交易笔数。")
    resolved = ontology.resolve(semantic)

    assert resolved["selected_tables"] == ["dwd_card_transaction", "dim_branch"]
    assert "txn_amount_cny" in resolved["selected_columns"]["dwd_card_transaction"]
    assert "transaction_id" in resolved["selected_columns"]["dwd_card_transaction"]


def test_lifecycle_policy_rejects_distractors(ontology: OntologyService) -> None:
    semantic = ontology.parse("查询近30天各分行信用卡交易金额和交易笔数。")
    resolved = ontology.resolve(semantic)
    rejected = {item.name: item for item in resolved["rejected_tables"]}

    assert rejected["legacy_card_transaction"].status == "DEPRECATED"
    assert rejected["legacy_card_transaction"].replacement == "dwd_card_transaction"
    assert rejected["tmp_transaction_result"].status == "TEMPORARY"
    assert rejected["test_transaction_copy"].status == "TEST"
    assert "无法支持交易 ID 去重计数" in rejected["dws_branch_transaction_day"].reason
