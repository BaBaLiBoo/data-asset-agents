import pytest

from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.models import QueryFilter, ValidationIssue
from data_asset_agents.validation import SQLRepairer, SQLValidator


def validator(ontology: OntologyService) -> SQLValidator:
    return SQLValidator(ontology.bundle)


def test_valid_read_only_query(ontology: OntologyService) -> None:
    report = validator(ontology).validate(
        "SELECT branch_id, COUNT(*) FROM dwd_card_transaction GROUP BY branch_id",
        {"dwd_card_transaction"},
    )

    assert report.valid
    assert report.read_only


def test_rejects_mutation(ontology: OntologyService) -> None:
    report = validator(ontology).validate(
        "DELETE FROM dwd_card_transaction",
        {"dwd_card_transaction"},
    )

    assert not report.valid
    assert {issue.code for issue in report.issues} >= {
        "NOT_SELECT",
        "PROHIBITED_OPERATION",
    }


def test_rejects_column_not_owned_by_table(ontology: OntologyService) -> None:
    report = validator(ontology).validate(
        "SELECT t.branch_name FROM dwd_card_transaction t",
        {"dwd_card_transaction"},
    )

    assert not report.valid
    assert "UNKNOWN_COLUMN" in {issue.code for issue in report.issues}


def test_rejects_unapproved_join(ontology: OntologyService) -> None:
    report = validator(ontology).validate(
        "SELECT b.branch_name FROM dwd_card_transaction t "
        "JOIN dim_branch b ON t.customer_id = b.branch_id",
        {"dwd_card_transaction", "dim_branch"},
    )

    assert not report.valid
    assert "UNAPPROVED_JOIN" in {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    "table",
    [
        "legacy_card_transaction",
        "tmp_transaction_result",
        "test_transaction_copy",
    ],
)
def test_rejects_lifecycle_forbidden_table_even_when_allowlisted(
    ontology: OntologyService,
    table: str,
) -> None:
    report = validator(ontology).validate(
        f"SELECT transaction_id FROM {table}",
        {table},
    )

    assert not report.valid
    assert "FORBIDDEN_LIFECYCLE_TABLE" in {
        issue.code for issue in report.issues
    }


def test_requires_metric_filters(ontology: OntologyService) -> None:
    report = validator(ontology).validate(
        "SELECT SUM(t.txn_amount_cny) FROM dwd_card_transaction t",
        {"dwd_card_transaction"},
        [
            QueryFilter(
                table="dwd_card_transaction",
                field="transaction_status",
                value="POSTED",
                source="metric_policy",
            )
        ],
    )

    assert not report.valid
    assert "MISSING_REQUIRED_FILTER" in {issue.code for issue in report.issues}


def test_deterministic_repair_adds_missing_filter(ontology: OntologyService) -> None:
    sql = "SELECT SUM(t.txn_amount_cny) FROM dwd_card_transaction t"
    issue = ValidationIssue(
        code="MISSING_REQUIRED_FILTER",
        message="missing",
        table="dwd_card_transaction",
        column="transaction_status",
        expected_value="POSTED",
    )

    repaired, _ = SQLRepairer().repair(sql, [issue])

    assert repaired is not None
    assert "transaction_status" in repaired
    assert "POSTED" in repaired


def test_deterministic_repair_refuses_unapproved_strategy() -> None:
    issue = ValidationIssue(code="UNKNOWN_COLUMN", message="unknown")

    repaired, reason = SQLRepairer().repair("SELECT 1", [issue])

    assert repaired is None
    assert "白名单" in reason
