import re
from pathlib import Path

from scripts.generate_upgrade_fixture import main as generate_upgrade_fixture

ROOT = Path(__file__).parents[1]
EXPECTED_DATABASE_FILES = [
    "data/ddl/001_schema.sql",
    "data/ddl/002_ontology_builder.sql",
    "data/ddl/003_release_safety_and_search.sql",
    "data/ddl/004_sql_assets.sql",
    "data/ddl/005_sql_asset_builds.sql",
    "data/ddl/006_evaluation.sql",
    "data/ddl/006_ontology_manager_core.sql",
    "data/ddl/007_ontology_runtime_governance.sql",
    "data/ddl/008_ontology_analysis_semantics.sql",
    "data/ddl/009_ontology_release_governance.sql",
    "data/ddl/010_ontology_construction.sql",
    "data/seed/002_seed.sql",
]


def _ordered_database_files(path: str) -> list[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    return re.findall(r"data/(?:ddl|seed)/[0-9][^\s:'\"]+\.sql", text)


def test_ci_and_compose_use_one_canonical_database_initialization_order() -> None:
    ci_files = _ordered_database_files(".github/workflows/ci.yml")
    compose_files = _ordered_database_files("docker-compose.yml")
    assert ci_files == EXPECTED_DATABASE_FILES
    assert compose_files == EXPECTED_DATABASE_FILES
    assert ci_files.count("data/ddl/009_ontology_release_governance.sql") == 1
    assert compose_files.count("data/ddl/009_ontology_release_governance.sql") == 1
    assert ci_files.count("data/ddl/010_ontology_construction.sql") == 1
    assert compose_files.count("data/ddl/010_ontology_construction.sql") == 1


def test_upgrade_fixture_is_fictional_pre_governance_state(capsys) -> None:
    assert generate_upgrade_fixture() == 0
    sql = capsys.readouterr().out
    assert "legacy-upgrade-version-id" in sql
    assert "legacy-upgrade-draft" in sql
    assert "legacy-upgrade-sql-build" in sql
    assert "legacy-upgrade-index-build" in sql
    assert "ontology_compiled_artifact" not in sql
    assert "ontology_audit_event" not in sql
    assert "secret" not in sql.lower()
    assert "password" not in sql.lower()


def test_upgrade_acceptance_acknowledges_legacy_breaking_impact() -> None:
    script = (ROOT / "scripts/upgrade_acceptance.ps1").read_text(encoding="utf-8")
    assert "acknowledge_breaking_changes = $true" in script
    assert 'change_ticket = "FICTIONAL-UPGRADE-ACCEPTANCE"' in script
