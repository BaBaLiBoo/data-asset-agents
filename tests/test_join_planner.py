import pytest

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import JoinDefinition
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.tools import JoinPlanner


def test_forward_join_is_oriented_from_connected_table(ontology: OntologyService) -> None:
    plan = JoinPlanner(ontology.bundle.joins).plan(
        ["dwd_card_transaction", "dim_branch"]
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].left_table == "dwd_card_transaction"
    assert plan.steps[0].right_table == "dim_branch"
    assert plan.steps[0].condition == (
        "dwd_card_transaction.branch_id = dim_branch.branch_id"
    )


def test_reverse_join_reverses_tables_columns_and_relationship(
    ontology: OntologyService,
) -> None:
    plan = JoinPlanner(ontology.bundle.joins).plan(
        ["dim_branch", "dwd_card_transaction"]
    )

    step = plan.steps[0]
    assert step.left_table == "dim_branch"
    assert step.right_table == "dwd_card_transaction"
    assert step.left_column == "branch_id"
    assert step.right_column == "branch_id"
    assert step.relationship == "one_to_many"


def test_multi_hop_join_adds_each_new_table_in_execution_order(
    ontology: OntologyService,
) -> None:
    plan = JoinPlanner(ontology.bundle.joins).plan(
        ["dim_account", "dwd_card_transaction"]
    )

    assert plan.tables == ["dim_account", "dim_card", "dwd_card_transaction"]
    assert [(step.left_table, step.right_table) for step in plan.steps] == [
        ("dim_account", "dim_card"),
        ("dim_card", "dwd_card_transaction"),
    ]


def test_join_planner_rejects_missing_path() -> None:
    planner = JoinPlanner(
        [
            JoinDefinition(
                left_table="table_a",
                right_table="table_b",
                left_column="id",
                right_column="a_id",
            )
        ]
    )

    with pytest.raises(OntologyError, match="No reviewed join path"):
        planner.plan(["table_a", "isolated_table"])
