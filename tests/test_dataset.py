from __future__ import annotations

from collections import Counter, defaultdict

from sqlglot import parse_one

from app.models import DatasetSplit, GoldLabel
from scripts.build_dataset import build_base_assets, build_pairs


def test_base_assets_are_24_and_sql_references_known_inputs() -> None:
    assets, _ = build_base_assets()
    assert len(assets) == 24
    assert len({asset.asset_id for asset in assets}) == 24
    for asset in assets:
        parse_one(asset.sql_text, read=asset.sql_dialect)
        assert asset.lineage.direct_upstreams
        assert asset.lineage.root_sources


def test_dataset_is_reference_based_with_reviewed_distribution() -> None:
    base_assets, _ = build_base_assets()
    all_assets, pairs = build_pairs(base_assets)
    ids = {asset.asset_id for asset in all_assets}
    splits = Counter(pair.split for pair in pairs)
    categories = Counter(
        "duplicate"
        if pair.label == GoldLabel.DUPLICATE
        else "similar"
        if pair.scenario.startswith("SIMILAR_")
        else "clear"
        for pair in pairs
    )
    assert len(pairs) == 60
    assert splits == {DatasetSplit.DEV: 40, DatasetSplit.TEST: 20}
    assert categories == {"duplicate": 20, "similar": 20, "clear": 20}
    assert all(pair.asset_a_id in ids and pair.asset_b_id in ids for pair in pairs)


def test_dataset_has_no_family_or_asset_split_leakage() -> None:
    base_assets, _ = build_base_assets()
    _, pairs = build_pairs(base_assets)
    family_splits: dict[str, set[DatasetSplit]] = defaultdict(set)
    asset_splits: dict[str, set[DatasetSplit]] = defaultdict(set)
    for pair in pairs:
        family_splits[pair.family_id].add(pair.split)
        asset_splits[pair.asset_a_id].add(pair.split)
        asset_splits[pair.asset_b_id].add(pair.split)
    assert all(len(values) == 1 for values in family_splits.values())
    assert all(len(values) == 1 for values in asset_splits.values())


def test_duplicate_variants_preserve_declared_grain_and_lineage() -> None:
    base_assets, _ = build_base_assets()
    all_assets, pairs = build_pairs(base_assets)
    by_id = {asset.asset_id: asset for asset in all_assets}
    for pair in pairs:
        if pair.label != GoldLabel.DUPLICATE:
            continue
        left = by_id[pair.asset_a_id]
        right = by_id[pair.asset_b_id]
        assert left.declared_grain == right.declared_grain
        assert left.lineage == right.lineage

