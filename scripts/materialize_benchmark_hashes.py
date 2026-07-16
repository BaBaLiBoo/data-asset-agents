"""Materialize Gold result hashes by executing reviewed SQL against MiniBank."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_asset_agents.core.config import get_settings
from data_asset_agents.evaluation.models import BenchmarkSuite
from data_asset_agents.evaluation.normalizer import ResultNormalizer
from data_asset_agents.execution.executor import QueryExecutor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/benchmark/text2sql_v1.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(args.path)
    hashes_path = path.with_name(f"{path.stem}_hashes.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    suite = BenchmarkSuite.model_validate(payload)
    executor = QueryExecutor(get_settings())
    normalizer = ResultNormalizer()
    mismatches: list[str] = []
    for index, case in enumerate(suite.cases):
        if case.expected_status != "success" or case.gold_sql is None:
            continue
        result = executor.execute(case.gold_sql, set(case.gold_tables))
        actual = normalizer.hash(result, order_sensitive=case.result_order_sensitive)
        if args.check:
            if actual != case.expected_result_hash:
                mismatches.append(
                    f"{case.id}: expected={case.expected_result_hash} actual={actual}"
                )
        else:
            payload["cases"][index]["expected_result_hash"] = actual
    if mismatches:
        print("\n".join(mismatches))
        return 1
    if not args.check:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reviewed = {
            item["id"]: item["expected_result_hash"]
            for item in payload["cases"]
            if item["expected_status"] == "success"
        }
        hashes_path.write_text(
            json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
