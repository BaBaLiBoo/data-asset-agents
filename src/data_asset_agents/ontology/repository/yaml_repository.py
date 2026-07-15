from pathlib import Path
from typing import Any

import yaml

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import OntologyBundle


class YamlOntologyRepository:
    """Load and validate the reviewed YAML semantic definition bundle."""

    REQUIRED_FILES = {
        "domain": "domain.yaml",
        "concepts": "concepts.yaml",
        "metrics": "metrics.yaml",
        "dimensions": "dimensions.yaml",
        "mappings": "mappings.yaml",
        "joins": "joins.yaml",
        "tables": "table_assets.yaml",
        "policies": "policies.yaml",
        "glossary": "glossary.yaml",
    }

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._bundle: OntologyBundle | None = None

    def load(self, *, force: bool = False) -> OntologyBundle:
        if self._bundle is not None and not force:
            return self._bundle
        raw: dict[str, Any] = {}
        for key, filename in self.REQUIRED_FILES.items():
            path = self.root / filename
            if not path.exists():
                raise OntologyError(f"Missing ontology file: {path}")
            try:
                raw[key] = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise OntologyError(f"Invalid YAML in {path}: {exc}") from exc
        try:
            self._bundle = OntologyBundle(**raw)
        except ValueError as exc:
            raise OntologyError(f"Ontology validation failed: {exc}") from exc
        return self._bundle

