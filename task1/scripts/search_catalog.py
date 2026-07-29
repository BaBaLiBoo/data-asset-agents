from __future__ import annotations

import argparse

from app.config import Settings
from app.models import SearchDuplicatesRequest
from app.service import Task1Service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search duplicate candidates for one asset.")
    parser.add_argument("asset_id")
    parser.add_argument("--candidate-top-k", type=int, default=20)
    parser.add_argument("--result-top-k", type=int, default=10)
    parser.add_argument("--all-domains", action="store_true")
    parser.add_argument("--include-not-duplicate", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = Task1Service.create(Settings.from_env())
    result = service.search_duplicates(
        SearchDuplicatesRequest(
            asset_id=args.asset_id,
            candidate_top_k=args.candidate_top_k,
            result_top_k=args.result_top_k,
            business_domain_only=not args.all_domains,
            include_not_duplicate=args.include_not_duplicate,
        )
    )
    print(f"Query: {result.query_asset_id} | {result.query_asset_name}")
    print(f"Catalog assets: {result.catalog_size}")
    print(f"Eligible candidates: {result.eligible_candidates}")
    print(f"Top-K reranked: {result.retrieved_candidates}")
    print("-" * 100)
    for item in result.results:
        comparison = item.comparison
        print(
            f"{item.candidate.asset_id} | {item.candidate.asset_name} | "
            f"{comparison.decision.value} | "
            f"recall={item.candidate.recall_score:.4f} | "
            f"fusion={comparison.fusion_score:.4f}"
        )
        print(
            f"  semantic={comparison.semantic.score:.4f} "
            f"logic={comparison.logic.score if comparison.logic.score is not None else 'NA'} "
            f"lineage={comparison.lineage.score if comparison.lineage.score is not None else 'NA'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
