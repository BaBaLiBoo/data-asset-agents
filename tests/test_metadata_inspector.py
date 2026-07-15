import pytest
from sqlalchemy import create_engine, text

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.metadata import MetadataInspector


@pytest.fixture
def metadata_inspector() -> MetadataInspector:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE parent (
                    parent_id INTEGER PRIMARY KEY,
                    parent_name TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE sample_asset (
                    asset_id INTEGER PRIMARY KEY,
                    parent_id INTEGER NOT NULL REFERENCES parent(parent_id),
                    asset_status TEXT,
                    amount NUMERIC
                )
                """
            )
        )
        connection.execute(text("CREATE INDEX idx_sample_status ON sample_asset(asset_status)"))
        connection.execute(text("INSERT INTO parent VALUES (1, 'Fictional Parent')"))
        connection.execute(
            text(
                """
                INSERT INTO sample_asset(asset_id, parent_id, asset_status, amount)
                VALUES (101, 1, 'ACTIVE', 10),
                       (102, 1, 'ACTIVE', 20),
                       (103, 1, NULL, 30)
                """
            )
        )
    return MetadataInspector(engine)


def test_metadata_and_profile_capture_is_complete_and_masked(
    metadata_inspector: MetadataInspector,
) -> None:
    snapshot = metadata_inspector.capture_snapshot(
        schema_name="main",
        allowed_tables={"sample_asset"},
        requested_tables=["sample_asset"],
        sample_limit=3,
        top_value_limit=3,
    )

    table = snapshot.tables[0]
    assert table.primary_key == ["asset_id"]
    assert table.foreign_keys[0].referred_table == "parent"
    assert table.indexes[0].name == "idx_sample_status"
    profiles = {profile.column_name: profile for profile in table.profiles}
    assert profiles["asset_status"].row_count == 3
    assert profiles["asset_status"].null_count == 1
    assert profiles["asset_status"].top_values[0].value == "ACTIVE"
    assert profiles["asset_id"].sample_values[0].startswith("***")
    assert profiles["amount"].minimum == "10"
    assert profiles["amount"].maximum == "30"


def test_metadata_inspector_rejects_non_allowlisted_and_unsafe_tables(
    metadata_inspector: MetadataInspector,
) -> None:
    with pytest.raises(OntologyError, match="not in the profiling allowlist"):
        metadata_inspector.capture_snapshot(
            schema_name="main",
            allowed_tables={"sample_asset"},
            requested_tables=["parent"],
        )

    with pytest.raises(OntologyError, match="Unsafe table identifier"):
        metadata_inspector.capture_snapshot(
            schema_name="main",
            allowed_tables={"sample_asset"},
            requested_tables=["sample_asset; DROP TABLE parent"],
        )
