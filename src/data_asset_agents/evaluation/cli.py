from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from data_asset_agents.core.config import get_settings
from data_asset_agents.evaluation.benchmark import load_benchmark, validate_benchmark
from data_asset_agents.evaluation.models import EvaluationRunRequest
from data_asset_agents.evaluation.normalizer import ResultNormalizer
from data_asset_agents.evaluation.runtime import build_evaluation_runtime
from data_asset_agents.validation import (
    CommonSQLSafetyValidator,
    OntologyPolicyValidator,
)


def _variant(mode: str, sql_assets: str) -> str:
    if mode != "ontology":
        return mode
    return "ontology_full" if sql_assets == "enabled" else "ontology_no_sql_asset"


def validate_command(args: argparse.Namespace) -> int:
    runtime = build_evaluation_runtime(get_settings())
    try:
        suite = load_benchmark(args.path)
        errors = validate_benchmark(
            suite,
            CommonSQLSafetyValidator(runtime.executor.catalog.business_only()),
            OntologyPolicyValidator(runtime.ontology.bundle, runtime.ontology.ontology_version_id),
            runtime.executor,
        )
        normalizer = ResultNormalizer()
        for case in suite.cases:
            if case.expected_status != "success" or not case.gold_sql:
                continue
            result = runtime.executor.execute(case.gold_sql, set(case.gold_tables))
            actual = normalizer.hash(result, order_sensitive=case.result_order_sensitive)
            if actual != case.expected_result_hash:
                errors.append(
                    f"{case.id}: result hash mismatch "
                    f"expected={case.expected_result_hash} actual={actual}"
                )
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"Benchmark {suite.version}: {len(suite.cases)} cases valid")
        return 0
    finally:
        runtime.engine.dispose()


def run_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    runtime = build_evaluation_runtime(settings)
    try:
        variant = _variant(args.mode, args.sql_assets)
        run = runtime.service.create_run(
            EvaluationRunRequest(
                query_mode=args.mode,
                strategy_variant=variant,
                sql_asset_enabled=variant == "ontology_full",
                benchmark_path=args.benchmark,
                max_cases=args.max_cases,
                run_kind=args.run_kind,
                concurrency=args.concurrency,
            )
        )
        completed = runtime.service.execute_run(run.run_id, args.benchmark)
        metrics = runtime.service.metrics(completed.run_id, args.benchmark)
        print(json.dumps(metrics.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if completed.status == "COMPLETED" else 1
    finally:
        runtime.engine.dispose()


def compare_command(args: argparse.Namespace) -> int:
    runtime = build_evaluation_runtime(get_settings())
    try:
        run_ids = (
            args.run_id
            or [
                run.run_id
                for run in runtime.service.repository.list_runs(20)
                if run.status == "COMPLETED"
            ][:4]
        )
        comparison = runtime.service.compare(
            run_ids,
            args.benchmark,
            allow_mismatch=args.allow_mismatch,
        )
        print(json.dumps(comparison.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0
    finally:
        runtime.engine.dispose()


def export_command(args: argparse.Namespace) -> int:
    runtime = build_evaluation_runtime(get_settings())
    try:
        cases = runtime.service.repository.list_cases(args.run_id)
        output = Path(args.output or f"evaluation-{args.run_id}.{args.format}")
        rows = [case.model_dump(mode="json") for case in cases]
        if args.format == "json":
            output.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            fieldnames = sorted({key for row in rows for key in row})
            with output.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            key: json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else value
                            for key, value in row.items()
                        }
                    )
        print(output)
        return 0
    finally:
        runtime.engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="data-asset-evaluation")
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-benchmark")
    validate.add_argument("--path", default="data/benchmark/text2sql_v1.json")
    validate.set_defaults(handler=validate_command)

    run = commands.add_parser("run")
    run.add_argument("--mode", choices=["schema", "rag", "ontology"], required=True)
    run.add_argument("--sql-assets", choices=["enabled", "disabled"], default="disabled")
    run.add_argument("--run-kind", choices=["smoke", "live"], required=True)
    run.add_argument("--benchmark", default="data/benchmark/text2sql_v1.json")
    run.add_argument("--max-cases", type=int)
    run.add_argument("--concurrency", type=int, default=1)
    run.set_defaults(handler=run_command)

    compare = commands.add_parser("compare")
    compare.add_argument("--run-id", action="append")
    compare.add_argument("--benchmark", default="data/benchmark/text2sql_v1.json")
    compare.add_argument("--allow-mismatch", action="store_true")
    compare.set_defaults(handler=compare_command)

    export = commands.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--output")
    export.set_defaults(handler=export_command)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handler: Any = args.handler
    try:
        return int(handler(args))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
