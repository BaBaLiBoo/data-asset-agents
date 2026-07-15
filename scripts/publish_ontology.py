from sqlalchemy import create_engine

from data_asset_agents.core.config import get_settings
from data_asset_agents.ontology.builder import OntologyPublisher
from data_asset_agents.ontology.repository import YamlOntologyRepository


def main() -> None:
    settings = get_settings()
    bundle = YamlOntologyRepository(settings.ontology_path).load()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    count = OntologyPublisher(engine).publish(bundle)
    print(f"Published {count} reviewed concepts from ontology version {bundle.domain['version']}")


if __name__ == "__main__":
    main()

