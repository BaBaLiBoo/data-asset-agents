"""Independent governed-catalog fixture; never loads a published ontology."""

from __future__ import annotations

import json
from pathlib import Path

from data_asset_agents.ontology.models import TableAsset


def load_governed_catalog(path: Path) -> list[TableAsset]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # owner/quality fields are catalog evidence but are intentionally not part of
    # the analytical TableAsset compatibility model.
    return [TableAsset.model_validate(item) for item in payload]
