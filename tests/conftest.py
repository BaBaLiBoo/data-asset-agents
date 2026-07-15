from pathlib import Path

import pytest

from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService


ROOT = Path(__file__).parents[1]


@pytest.fixture
def ontology() -> OntologyService:
    return OntologyService(YamlOntologyRepository(ROOT / "ontology/retail_banking"))

