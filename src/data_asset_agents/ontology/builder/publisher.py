import json

from sqlalchemy import Engine, text

from data_asset_agents.ontology.models import OntologyBundle


class OntologyPublisher:
    """Publish reviewed YAML concepts to the PostgreSQL runtime repository."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(self, bundle: OntologyBundle) -> int:
        concepts = [
            *bundle.concepts,
            *[
                {
                    "id": metric.id,
                    "name": metric.name,
                    "kind": "metric",
                    "description": metric.description,
                    "synonyms": metric.synonyms,
                }
                for metric in bundle.metrics
            ],
            *[
                {
                    "id": dimension.id,
                    "name": dimension.name,
                    "kind": "dimension",
                    "description": dimension.description,
                    "synonyms": dimension.synonyms,
                }
                for dimension in bundle.dimensions
            ],
        ]
        version = bundle.domain["version"]
        with self.engine.begin() as connection:
            for item in concepts:
                data = item.model_dump() if hasattr(item, "model_dump") else item
                data["id"] = f"{data['kind']}:{data['id']}"
                connection.execute(
                    text(
                        """
                        INSERT INTO semantic_concept
                            (concept_id, name, kind, description, synonyms, ontology_version)
                        VALUES (:id, :name, :kind, :description, CAST(:synonyms AS jsonb), :version)
                        ON CONFLICT (concept_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            kind = EXCLUDED.kind,
                            description = EXCLUDED.description,
                            synonyms = EXCLUDED.synonyms,
                            ontology_version = EXCLUDED.ontology_version,
                            published_at = now()
                        """
                    ),
                    {**data, "synonyms": json.dumps(data.get("synonyms", []), ensure_ascii=False), "version": version},
                )
        return len(concepts)
