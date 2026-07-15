from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.tools import JoinPlanner


def test_join_path_uses_reviewed_edge(ontology: OntologyService) -> None:
    plan = JoinPlanner(ontology.bundle.joins).plan(["dwd_card_transaction", "dim_branch"])

    assert len(plan.steps) == 1
    assert plan.steps[0].condition == (
        "dwd_card_transaction.branch_id = dim_branch.branch_id"
    )


def test_join_path_can_cross_multiple_tables(ontology: OntologyService) -> None:
    plan = JoinPlanner(ontology.bundle.joins).plan(["dwd_card_transaction", "dim_account"])

    assert len(plan.steps) == 2
    assert "dim_card" in plan.tables

