"""Run the formal eight-group reviewed-ontology Text-to-SQL experiment."""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GROUPS: tuple[dict[str, Any], ...] = (
    {
        "group": "T-A",
        "mode": "schema",
        "variant": "schema",
        "sql_assets": False,
        "source": "NONE",
    },
    {
        "group": "T-B",
        "mode": "rag",
        "variant": "rag",
        "sql_assets": False,
        "source": "NONE",
    },
    {
        "group": "T-C",
        "mode": "ontology",
        "variant": "ontology_no_sql_asset",
        "sql_assets": False,
        "source": "REVIEWED_O_C",
    },
    {
        "group": "T-D",
        "mode": "ontology",
        "variant": "ontology_full",
        "sql_assets": True,
        "source": "REVIEWED_O_C",
    },
    {
        "group": "T-E",
        "mode": "ontology",
        "variant": "ontology_no_sql_asset",
        "sql_assets": False,
        "source": "REVIEWED_O_D",
    },
    {
        "group": "T-F",
        "mode": "ontology",
        "variant": "ontology_full",
        "sql_assets": True,
        "source": "REVIEWED_O_D",
    },
    {
        "group": "T-G",
        "mode": "ontology",
        "variant": "ontology_no_sql_asset",
        "sql_assets": False,
        "source": "GOLD",
    },
    {
        "group": "T-H",
        "mode": "ontology",
        "variant": "ontology_full",
        "sql_assets": True,
        "source": "GOLD",
    },
)


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 300,
) -> Any:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _activate(base_url: str, version: str) -> dict[str, Any]:
    return _request(
        base_url,
        f"/api/v1/ontology/versions/{version}/activate",
        method="POST",
        payload={},
        headers={"X-Actor": "quality-v2-formal-runner"},
        timeout=600,
    )


def _wait_run(
    base_url: str,
    run_id: str,
    *,
    deadline_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        response = _request(base_url, f"/api/v1/evaluation/runs/{run_id}")
        status = response["run"]["status"]
        if status == "COMPLETED":
            return response
        if status == "FAILED":
            raise RuntimeError(
                f"Evaluation {run_id} failed: "
                f"{response['run'].get('error_message') or 'unknown error'}"
            )
        time.sleep(5)
    raise TimeoutError(f"Evaluation {run_id} exceeded {deadline_seconds}s")


def _write_cases(path: Path, cases: list[dict[str, Any]]) -> None:
    fields = sorted({field for case in cases for field in case})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    field: (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list))
                        else value
                    )
                    for field, value in case.items()
                }
            )


def _write_comparison_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["experiment_group", "ontology_source", "ontology_version_id"]
    metric_fields = sorted(
        {
            field
            for row in rows
            for field in row["metrics"]
            if field not in {"run_id", "failure_distribution", "token_usage"}
        }
    )
    fields.extend(metric_fields)
    fields.extend(
        [
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "failure_distribution",
        ]
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            metrics = row["metrics"]
            writer.writerow(
                {
                    "experiment_group": row["run"]["experiment_group"],
                    "ontology_source": row["run"]["ontology_source"],
                    "ontology_version_id": row["run"]["ontology_version_id"],
                    **{field: metrics.get(field) for field in metric_fields},
                    "input_tokens": metrics["token_usage"]["input_tokens"],
                    "output_tokens": metrics["token_usage"]["output_tokens"],
                    "total_tokens": metrics["token_usage"]["total_tokens"],
                    "failure_distribution": json.dumps(
                        metrics["failure_distribution"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )


def _error_analysis(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Text-to-SQL Quality V2 Error Analysis",
        "",
        "All counts below come from completed persisted Case results. "
        "No mock cases are substituted.",
        "",
        "| Group | Cases | Successful cases | Result hash accuracy | Main failures |",
        "|---|---:|---:|---:|---|",
    ]
    for row in rows:
        cases = row["cases"]
        failures = Counter(
            case["failure_category"]
            for case in cases
            if case.get("failure_category")
        )
        rendered = ", ".join(f"{key}: {value}" for key, value in failures.most_common())
        lines.append(
            f"| {row['run']['experiment_group']} | {len(cases)} | "
            f"{sum(bool(case['success']) for case in cases)} | "
            f"{row['metrics']['result_hash_accuracy']} | {rendered or '—'} |"
        )
    lines.extend(
        [
            "",
            "`sql_asset_selection_accuracy` is `null` because the benchmark "
            "does not define a Gold SQLAsset ID per case. Template adoption and "
            "result-correct template rewrite are reported separately.",
        ]
    )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    versions = {
        "REVIEWED_O_C": {
            "version": args.o_c_version,
            "construction_run_id": args.o_c_run,
        },
        "REVIEWED_O_D": {
            "version": args.o_d_version,
            "construction_run_id": args.o_d_run,
        },
        "GOLD": {"version": args.gold_version, "construction_run_id": None},
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for source, provenance in versions.items():
        activated = _activate(args.base_url, provenance["version"])
        artifact = _request(
            args.base_url,
            f"/api/v1/ontology/versions/{activated['id']}/compiled-artifact",
        )
        artifacts[source] = {
            "ontology_version_id": activated["id"],
            "version": activated["version"],
            "artifact_id": artifact["artifact_id"],
            "artifact_hash": artifact["source_resource_hash"],
            "bundle_hash": artifact["bundle_hash"],
            "construction_run_id": artifact.get("construction_run_id"),
        }
    if len({item["ontology_version_id"] for item in artifacts.values()}) != 3:
        raise RuntimeError("O-C, O-D, and Gold ontology version IDs are not isolated")
    if len({item["bundle_hash"] for item in artifacts.values()}) != 3:
        raise RuntimeError("O-C, O-D, and Gold bundle hashes are not isolated")
    if len({item["artifact_hash"] for item in artifacts.values()}) != 3:
        raise RuntimeError("O-C, O-D, and Gold artifact hashes are not isolated")

    rows: list[dict[str, Any]] = []
    active_source: str | None = None
    for spec in GROUPS:
        source = spec["source"]
        if source != "NONE" and source != active_source:
            _activate(args.base_url, versions[source]["version"])
            active_source = source
        payload = {
            "query_mode": spec["mode"],
            "strategy_variant": spec["variant"],
            "sql_asset_enabled": spec["sql_assets"],
            "benchmark_path": "data/benchmark/text2sql_v1.json",
            "run_kind": "live",
            "concurrency": args.concurrency,
            "experiment_group": spec["group"],
            "ontology_source": source,
            "construction_run_id": (
                versions[source]["construction_run_id"] if source != "NONE" else None
            ),
        }
        created = _request(
            args.base_url,
            "/api/v1/evaluation/runs",
            method="POST",
            payload=payload,
        )
        print(f"{spec['group']} started: {created['run_id']}", flush=True)
        completed = _wait_run(
            args.base_url,
            created["run_id"],
            deadline_seconds=args.run_timeout,
        )
        cases = _request(
            args.base_url,
            f"/api/v1/evaluation/runs/{created['run_id']}/cases",
        )
        if len(cases) != 80:
            raise RuntimeError(
                f"{spec['group']} persisted {len(cases)} cases instead of 80"
            )
        row = {
            "run": completed["run"],
            "metrics": completed["metrics"],
            "cases": cases,
        }
        rows.append(row)
        _write_cases(output / f"{spec['group']}.csv", cases)
        print(
            f"{spec['group']} completed: "
            f"result_accuracy={completed['metrics']['result_hash_accuracy']}",
            flush=True,
        )

    sql_builds = {
        row["run"]["ontology_source"]: row["run"]["sql_asset_build_id"]
        for row in rows
        if row["run"]["strategy_variant"] == "ontology_full"
    }
    if len(sql_builds) != 3 or len(set(sql_builds.values())) != 3:
        raise RuntimeError("O-C, O-D, and Gold SQLAsset builds are not isolated")

    query = urlencode(
        [("run_id", row["run"]["run_id"]) for row in rows]
    )
    api_comparison = _request(
        args.base_url,
        f"/api/v1/evaluation/compare?{query}",
        timeout=600,
    )
    manifest = {
        "evaluation_schema_version": "2.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "benchmark_case_count_per_group": 80,
        "total_case_count": 640,
        "concurrency": args.concurrency,
        "artifacts": artifacts,
        "sql_asset_builds": sql_builds,
        "runs": [row["run"] for row in rows],
    }
    comparison = {
        "evaluation_schema_version": "2.0",
        "api_comparison": api_comparison,
        "groups": [
            {"run": row["run"], "metrics": row["metrics"]} for row in rows
        ],
    }
    correlations: dict[str, Any] = {
        "generated_at": manifest["generated_at"],
        "construction_runs": {},
        "query_failures": {},
    }
    for source in ("REVIEWED_O_C", "REVIEWED_O_D"):
        run_id = versions[source]["construction_run_id"]
        evaluation = _request(
            args.base_url,
            f"/api/v1/ontology/construction-runs/{run_id}/evaluation",
        )
        correlations["construction_runs"][source] = {
            "construction_run_id": run_id,
            "error_analysis": evaluation["error_analysis"],
            "review_cost": evaluation["review_cost"],
            "review_delta": evaluation["review_delta"],
        }
    for row in rows:
        correlations["query_failures"][row["run"]["experiment_group"]] = [
            {
                "case_id": case["case_id"],
                "failure_category": case["failure_category"],
                "failure_reason": case["failure_reason"],
                "ontology_source": row["run"]["ontology_source"],
                "construction_run_id": row["run"]["construction_run_id"],
            }
            for case in row["cases"]
            if case.get("failure_category")
        ]

    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_comparison_csv(output / "comparison.csv", rows)
    (output / "error-analysis.md").write_text(
        _error_analysis(rows),
        encoding="utf-8",
    )
    (output / "construction-query-correlation.json").write_text(
        json.dumps(correlations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--base-url", default="http://localhost:8000")
    root.add_argument(
        "--output",
        type=Path,
        default=Path("reports/text2sql_reviewed_ontology_v2"),
    )
    root.add_argument("--o-c-version", required=True)
    root.add_argument("--o-c-run", required=True)
    root.add_argument("--o-d-version", required=True)
    root.add_argument("--o-d-run", required=True)
    root.add_argument("--gold-version", required=True)
    root.add_argument("--concurrency", type=int, default=2)
    root.add_argument("--run-timeout", type=int, default=7200)
    return root


if __name__ == "__main__":
    run(parser().parse_args())
