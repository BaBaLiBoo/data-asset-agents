from data_asset_agents.core.config import Settings
from data_asset_agents.ontology.builder import CandidateGenerator
from data_asset_agents.ontology.models import (
    ColumnMetadata,
    ColumnProfile,
    HistoricalSQLSummary,
    MetadataSnapshot,
    TableMetadata,
)


def test_mock_candidate_generation_is_stable_and_never_verified() -> None:
    profile = ColumnProfile(
        table_name="dwd_card_transaction",
        column_name="txn_amount_cny",
        data_type="NUMERIC(14, 2)",
        row_count=10,
        null_count=0,
        null_rate=0,
        distinct_count=9,
        unique_rate=0.9,
        minimum="10.00",
        maximum="100.00",
        sample_values=["10.00", "20.00"],
    )
    snapshot = MetadataSnapshot(
        id="snapshot_test",
        schema_name="public",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name="dwd_card_transaction",
                columns=[
                    ColumnMetadata(
                        name="txn_amount_cny",
                        data_type="NUMERIC(14, 2)",
                        nullable=False,
                    )
                ],
                profiles=[profile],
            )
        ],
    )
    generator = CandidateGenerator(Settings(llm_mode="mock"))

    first = generator.generate(snapshot, [], HistoricalSQLSummary(parsed_count=0))
    second = generator.generate(snapshot, [], HistoricalSQLSummary(parsed_count=0))

    assert first == second
    concept = first[0][0]
    assert concept.business_name == "人民币交易金额"
    assert concept.role == "measure"
    assert concept.unit == "CNY"
    assert concept.status.value == "CANDIDATE"
    assert first[1][0].status.value == "CANDIDATE"
