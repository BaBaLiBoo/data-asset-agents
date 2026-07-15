from pathlib import Path

import pytest
from sqlalchemy import create_engine

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.metadata import MetadataInspector
from data_asset_agents.ontology.builder import CandidateGenerator, OntologyBuildService
from data_asset_agents.ontology.models import OntologyPublishRequest
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.validation import OntologyContractValidator
from data_asset_agents.sql_assets import HistoricalSQLParser

ROOT = Path(__file__).parents[1]


class InvalidDryRunRepository:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.publish_called = False

    def prepare_publication(self, seed_bundle, _request):
        return seed_bundle, [], []

    def publish(self, _seed_bundle, _request):
        self.publish_called = True
        raise AssertionError("repository publication must not be reached")


def test_failed_dry_run_blocks_formal_publication() -> None:
    settings = Settings(llm_mode="mock")
    yaml_repository = YamlOntologyRepository(ROOT / "ontology/retail_banking")
    repository = InvalidDryRunRepository()
    builder = OntologyBuildService(
        inspector=MetadataInspector(repository.engine),
        sql_parser=HistoricalSQLParser(),
        generator=CandidateGenerator(settings),
        repository=repository,  # type: ignore[arg-type]
        seed_bundle=yaml_repository.load(),
        historical_sql_path=settings.historical_sql_path,
        yaml_repository=yaml_repository,
    )
    builder.contract_validator = OntologyContractValidator()
    with pytest.raises(OntologyError, match="dry run failed"):
        builder.publish(
            OntologyPublishRequest(
                version="blocked-1",
                published_by="test",
                snapshot_id="snapshot-one",
            )
        )
    assert not repository.publish_called
