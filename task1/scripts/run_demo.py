from __future__ import annotations

import json

from app.config import PROJECT_ROOT, Settings
from app.evaluation import load_assets, load_pairs
from app.models import ComparisonMode, ComparisonRequest
from app.service import Task1Service


def main() -> int:
    settings = Settings.from_env()
    service = Task1Service.create(settings)
    assets = load_assets(PROJECT_ROOT / "data" / "assets.jsonl")
    pairs = {pair.pair_id: pair for pair in load_pairs(PROJECT_ROOT / "data" / "pairs.csv")}
    cases = json.loads((PROJECT_ROOT / "data" / "demo_cases.json").read_text(encoding="utf-8"))
    print(
        f"Embedding model: {service.embedding_model} "
        f"(dimension={service.embedding_dimension})"
    )
    print("=" * 100)
    for case in cases:
        pair = pairs[case["pair_id"]]
        print(f"{pair.pair_id} | {case['asset_a_name']}  <->  {case['asset_b_name']}")
        print(f"场景={pair.scenario} | 标准标签={pair.label.value}")
        for mode in (ComparisonMode.KEYWORD_BASELINE, ComparisonMode.THREE_LAYER):
            result = service.compare(
                ComparisonRequest(
                    mode=mode,
                    asset_a=assets[pair.asset_a_id],
                    asset_b=assets[pair.asset_b_id],
                )
            )
            score = result.keyword_score if mode == ComparisonMode.KEYWORD_BASELINE else result.fusion_score
            print(
                f"  {mode.value:<17} score={score:.4f} decision={result.decision.value} "
                f"coverage={result.evidence_coverage:.4f}"
            )
            if mode == ComparisonMode.THREE_LAYER:
                logic_text = "NA" if result.logic.score is None else f"{result.logic.score:.4f}"
                lineage_text = (
                    "NA" if result.lineage.score is None else f"{result.lineage.score:.4f}"
                )
                print(
                    f"    semantic={result.semantic.score:.4f} "
                    f"logic={logic_text} lineage={lineage_text}"
                )
        print("-" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
