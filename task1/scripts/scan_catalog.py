from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.models import FullScanRequest
from app.service import Task1Service


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Top-K recall and three-layer rerank over the full asset catalog."
    )
    parser.add_argument("--candidate-top-k", type=int, default=20)
    parser.add_argument("--all-domains", action="store_true")
    parser.add_argument("--include-not-duplicate", action="store_true")
    parser.add_argument("--max-assets", type=int, default=None)
    return parser.parse_args()


def pair_key(left: str, right: str) -> str:
    return "|".join(sorted((left, right)))


def benchmark_check(result) -> dict[str, object]:
    pairs_path = PROJECT_ROOT / "data" / "pairs.csv"
    if not pairs_path.exists():
        return {}
    with pairs_path.open("r", encoding="utf-8-sig", newline="") as handle:
        known = {
            pair_key(row["asset_a_id"], row["asset_b_id"])
            for row in csv.DictReader(handle)
            if row["label"] == "DUPLICATE"
        }
    detected = {
        pair_key(pair.asset_a_id, pair.asset_b_id)
        for pair in result.pairs
        if pair.comparison.decision.value == "DUPLICATE"
    }
    hits = known & detected
    return {
        "known_duplicate_count": len(known),
        "known_duplicate_detected": len(hits),
        "known_duplicate_end_to_end_recall": len(hits) / len(known) if known else None,
        "missing_known_duplicate_pairs": sorted(known - detected),
        "unlisted_detected_duplicate_pairs": sorted(detected - known),
        "boundary": "controlled pair labels; not real-bank accuracy evidence",
    }


def write_reports(result, report_dir: Path, benchmark: dict[str, object]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "full_scan.json").write_text(
        result.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    with (report_dir / "pairs.csv").open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        fieldnames = [
            "asset_a_id",
            "asset_a_name",
            "asset_b_id",
            "asset_b_name",
            "recall_score",
            "decision",
            "fusion_score",
            "semantic_score",
            "logic_score",
            "lineage_score",
            "evidence_coverage",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for pair in result.pairs:
            comparison = pair.comparison
            writer.writerow(
                {
                    "asset_a_id": pair.asset_a_id,
                    "asset_a_name": pair.asset_a_name,
                    "asset_b_id": pair.asset_b_id,
                    "asset_b_name": pair.asset_b_name,
                    "recall_score": pair.recall_score,
                    "decision": comparison.decision.value,
                    "fusion_score": comparison.fusion_score,
                    "semantic_score": comparison.semantic.score,
                    "logic_score": comparison.logic.score,
                    "lineage_score": comparison.lineage.score,
                    "evidence_coverage": comparison.evidence_coverage,
                }
            )
    summary = {
        "catalog_size": result.catalog_size,
        "scanned_assets": result.scanned_assets,
        "naive_pair_count": result.naive_pair_count,
        "directed_candidate_count": result.candidate_pair_count,
        "compared_pair_count": result.compared_pair_count,
        "comparison_reduction_rate": result.comparison_reduction_rate,
        "duplicate_count": result.duplicate_count,
        "suspected_count": result.suspected_count,
        "returned_pair_count": result.returned_pair_count,
        "controlled_benchmark": benchmark,
    }
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "benchmark_check.json").write_text(
        json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    service = Task1Service.create(Settings.from_env())
    result = service.scan_all(
        FullScanRequest(
            candidate_top_k=args.candidate_top_k,
            business_domain_only=not args.all_domains,
            include_suspected=True,
            include_not_duplicate=args.include_not_duplicate,
            max_assets=args.max_assets,
        )
    )
    report_dir = PROJECT_ROOT / "reports" / "full_scan" / "latest"
    benchmark = benchmark_check(result)
    write_reports(result, report_dir, benchmark)
    print(
        f"Embedding model: {service.embedding_model} "
        f"(dimension={service.embedding_dimension})"
    )
    print(f"Catalog assets: {result.catalog_size}")
    print(f"Scanned assets: {result.scanned_assets}")
    print(f"Naive all-pairs comparisons: {result.naive_pair_count}")
    print(f"Directed Top-K candidate hits: {result.candidate_pair_count}")
    print(f"Top-K candidate pairs reranked: {result.compared_pair_count}")
    print(f"Expensive comparison reduction: {result.comparison_reduction_rate:.2%}")
    print(f"Duplicate pairs: {result.duplicate_count}")
    print(f"Suspected pairs: {result.suspected_count}")
    if benchmark:
        print(
            "Controlled known duplicates detected: "
            f"{benchmark['known_duplicate_detected']}/"
            f"{benchmark['known_duplicate_count']}"
        )
    print(f"Report: {report_dir}")
    print("-" * 100)
    for pair in result.pairs[:20]:
        print(
            f"{pair.asset_a_id} <-> {pair.asset_b_id} | "
            f"{pair.comparison.decision.value} | "
            f"recall={pair.recall_score:.4f} | "
            f"fusion={pair.comparison.fusion_score:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
