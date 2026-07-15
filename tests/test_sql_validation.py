from data_asset_agents.validation import SQLValidator


def test_valid_read_only_query() -> None:
    report = SQLValidator().validate(
        "SELECT branch_id, COUNT(*) FROM dwd_card_transaction GROUP BY branch_id",
        {"dwd_card_transaction"},
    )

    assert report.valid
    assert report.read_only


def test_rejects_mutation() -> None:
    report = SQLValidator().validate(
        "DELETE FROM dwd_card_transaction",
        {"dwd_card_transaction"},
    )

    assert not report.valid
    assert any("SELECT" in error or "Prohibited" in error for error in report.errors)


def test_rejects_non_selected_table() -> None:
    report = SQLValidator().validate("SELECT * FROM legacy_card_transaction", {"dim_branch"})

    assert not report.valid
    assert "legacy_card_transaction" in " ".join(report.errors)

