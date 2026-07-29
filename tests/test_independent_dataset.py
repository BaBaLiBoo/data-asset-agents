from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlglot import parse_one

from app.config import PROJECT_ROOT
from app.evaluation import load_assets, load_pairs
from app.models import DatasetSplit, GoldLabel


DATASET_DIR = PROJECT_ROOT / "data" / "evaluation_v3"
GENERATOR_PATH = PROJECT_ROOT / "scripts" / "build_independent_dataset.py"


def test_independent_dataset_has_reviewed_size_and_balance() -> None:
    assets = load_assets(DATASET_DIR / "assets.jsonl")
    pairs = load_pairs(DATASET_DIR / "pairs.csv")
    manifest = json.loads(
        (DATASET_DIR / "dataset_manifest.json").read_text(encoding="utf-8")
    )

    assert len(assets) == 360
    assert len(pairs) == 270
    assert Counter(pair.label for pair in pairs) == {
        GoldLabel.DUPLICATE: 90,
        GoldLabel.NOT_DUPLICATE: 180,
    }
    assert Counter(pair.split for pair in pairs) == {
        DatasetSplit.DEV: 90,
        DatasetSplit.TEST: 180,
    }
    assert Counter(
        "duplicate"
        if pair.label == GoldLabel.DUPLICATE
        else "hard"
        if pair.scenario.startswith("HARD_")
        else "clear"
        for pair in pairs
    ) == {"duplicate": 90, "hard": 90, "clear": 90}
    assert manifest["asset_count"] == 360
    assert manifest["pair_count"] == 270
    assert manifest["asset_split_leakage"] == 0
    assert manifest["family_split_leakage"] == 0


def test_independent_dataset_references_valid_parseable_assets() -> None:
    assets = load_assets(DATASET_DIR / "assets.jsonl")
    pairs = load_pairs(DATASET_DIR / "pairs.csv")

    assert all(
        pair.asset_a_id in assets and pair.asset_b_id in assets for pair in pairs
    )
    for asset in assets.values():
        parse_one(asset.sql_text, read=asset.sql_dialect)
        assert asset.lineage.direct_upstreams
        assert asset.lineage.root_sources
        assert asset.lineage.column_signatures


def test_independent_dataset_has_no_asset_or_family_split_leakage() -> None:
    pairs = load_pairs(DATASET_DIR / "pairs.csv")
    asset_splits: dict[str, set[DatasetSplit]] = defaultdict(set)
    family_splits: dict[str, set[DatasetSplit]] = defaultdict(set)
    for pair in pairs:
        family_splits[pair.family_id].add(pair.split)
        asset_splits[pair.asset_a_id].add(pair.split)
        asset_splits[pair.asset_b_id].add(pair.split)

    assert len(family_splits) == 90
    assert all(len(splits) == 1 for splits in family_splits.values())
    assert all(len(splits) == 1 for splits in asset_splits.values())
    assert sum(
        next(iter(splits)) == DatasetSplit.DEV
        for splits in family_splits.values()
    ) == 30
    assert sum(
        next(iter(splits)) == DatasetSplit.TEST
        for splits in family_splits.values()
    ) == 60


def test_dev_and_test_use_disjoint_transformation_templates() -> None:
    pairs = load_pairs(DATASET_DIR / "pairs.csv")
    dev_scenarios = {
        pair.scenario for pair in pairs if pair.split == DatasetSplit.DEV
    }
    test_scenarios = {
        pair.scenario for pair in pairs if pair.split == DatasetSplit.TEST
    }

    assert "DUPLICATE_CTE_REWRITE" in dev_scenarios
    assert "DUPLICATE_SUBQUERY_REWRITE" not in dev_scenarios
    assert {"HARD_AGGREGATION_CONFLICT", "HARD_FILTER_CONFLICT"} <= dev_scenarios

    assert "DUPLICATE_SUBQUERY_REWRITE" in test_scenarios
    assert "DUPLICATE_CTE_REWRITE" not in test_scenarios
    assert {
        "HARD_TIME_GRAIN_CONFLICT",
        "HARD_DATE_FIELD_CONFLICT",
        "HARD_MEASURE_CONFLICT",
        "HARD_CURRENCY_SCOPE_CONFLICT",
    } <= test_scenarios

    assert not {
        scenario
        for scenario in dev_scenarios & test_scenarios
        if scenario != "CLEAR_CROSS_DOMAIN"
    }


def test_generator_does_not_depend_on_comparison_or_threshold_modules() -> None:
    source = GENERATOR_PATH.read_text(encoding="utf-8")
    forbidden_imports = (
        "app.embedding",
        "app.semantic",
        "app.keyword",
        "app.sql_logic",
        "app.lineage",
        "app.fusion",
        "app.service",
        "app.evaluation",
    )
    assert all(name not in source for name in forbidden_imports)
    assert "suspected_threshold" not in source
    assert "duplicate_threshold" not in source
    assert "semantic_weight" not in source
    assert "logic_weight" not in source
    assert "lineage_weight" not in source


def test_dataset_files_match_frozen_manifest_hashes() -> None:
    import hashlib

    manifest = json.loads(
        (DATASET_DIR / "dataset_manifest.json").read_text(encoding="utf-8")
    )

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert sha256(DATASET_DIR / "assets.jsonl") == manifest["assets_sha256"]
    assert sha256(DATASET_DIR / "pairs.csv") == manifest["pairs_sha256"]
