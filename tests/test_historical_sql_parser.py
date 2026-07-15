from data_asset_agents.sql_assets import HistoricalSQLParser


def test_history_parser_extracts_structured_query_evidence() -> None:
    sql = """
        SELECT b.branch_name,
               SUM(t.txn_amount_cny) AS amount
        FROM dwd_card_transaction t
        JOIN dim_branch b ON t.branch_id = b.branch_id
        WHERE t.transaction_status = 'POSTED'
          AND t.transaction_date >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY b.branch_name
    """

    analysis = HistoricalSQLParser().parse(sql, question="test", certified=True)

    assert analysis.tables == ["dwd_card_transaction", "dim_branch"]
    assert analysis.joins[0].expression == (
        "dwd_card_transaction.branch_id = dim_branch.branch_id"
    )
    assert analysis.aggregates[0].function == "SUM"
    assert analysis.group_by == ["b.branch_name"]
    assert analysis.time_fields[0].column == "transaction_date"
    assert len(analysis.filters) == 2


def test_history_summary_counts_cooccurrence_columns_and_joins() -> None:
    parser = HistoricalSQLParser()
    analysis = parser.parse(
        "SELECT t.transaction_id FROM dwd_card_transaction t "
        "JOIN dim_branch b ON t.branch_id = b.branch_id"
    )

    summary = parser.summarize([analysis, analysis])

    assert summary.table_cooccurrence[0].count == 2
    assert summary.join_usage[0].count == 2
    assert any(item.key.endswith("transaction_id") for item in summary.column_usage)
