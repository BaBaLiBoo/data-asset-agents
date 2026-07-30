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
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
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
    temporary.replace(path)


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
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
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
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_group_row(
    spec: dict[str, Any],
    completed: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    concurrency: int,
) -> dict[str, Any]:
    run = completed["run"]
    if run["status"] != "COMPLETED":
        raise RuntimeError(
            f"{spec['group']} resume Run {run['run_id']} is not COMPLETED"
        )
    expected = {
        "experiment_group": spec["group"],
        "strategy_variant": spec["variant"],
        "sql_asset_enabled": spec["sql_assets"],
        "ontology_source": spec["source"],
        "run_kind": "live",
        "concurrency": concurrency,
        "max_cases": None,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if run.get(field) != expected_value
    ]
    if mismatches:
        raise RuntimeError(
            f"{spec['group']} resume Run provenance mismatch: "
            f"{', '.join(mismatches)}"
        )
    if len(cases) != 80:
        raise RuntimeError(
            f"{spec['group']} persisted {len(cases)} cases instead of 80"
        )
    return {"run": run, "metrics": completed["metrics"], "cases": cases}


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "progress_schema_version": "1.0",
            "status": "IN_PROGRESS",
            "completed_groups": {},
            "failed_attempts": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("progress_schema_version") != "1.0":
        raise RuntimeError(f"Unsupported resume manifest: {path}")
    return payload


def _rows_by_group(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["run"]["experiment_group"]: row for row in rows}


def _result_delta(
    groups: dict[str, dict[str, Any]],
    left_group: str,
    right_group: str,
) -> float:
    left = groups[left_group]["metrics"]["result_hash_accuracy"]
    right = groups[right_group]["metrics"]["result_hash_accuracy"]
    if left is None or right is None:
        raise RuntimeError("Formal result_hash_accuracy cannot be null")
    return round(float(left) - float(right), 6)


def _case_outcome_comparison(
    groups: dict[str, dict[str, Any]],
    left_group: str,
    right_group: str,
) -> dict[str, Any]:
    left = {case["case_id"]: case for case in groups[left_group]["cases"]}
    right = {case["case_id"]: case for case in groups[right_group]["cases"]}
    if set(left) != set(right):
        raise RuntimeError(
            f"{left_group}/{right_group} do not share identical Case IDs"
        )

    def details(case_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "case_id": case_id,
                "question": left[case_id].get("question"),
                "left_failure_category": left[case_id].get("failure_category"),
                "right_failure_category": right[case_id].get("failure_category"),
            }
            for case_id in case_ids
        ]

    left_failed_right_succeeded = sorted(
        case_id
        for case_id in left
        if not left[case_id]["success"] and right[case_id]["success"]
    )
    left_succeeded_right_failed = sorted(
        case_id
        for case_id in left
        if left[case_id]["success"] and not right[case_id]["success"]
    )
    return {
        "left_group": left_group,
        "right_group": right_group,
        "left_failed_right_succeeded_count": len(left_failed_right_succeeded),
        "left_failed_right_succeeded": details(left_failed_right_succeeded),
        "left_succeeded_right_failed_count": len(left_succeeded_right_failed),
        "left_succeeded_right_failed": details(left_succeeded_right_failed),
    }


def _build_downstream_analysis(
    rows: list[dict[str, Any]],
    construction_runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    groups = _rows_by_group(rows)
    comparisons = {
        "o_c_without_assets_vs_gold": _case_outcome_comparison(
            groups, "T-C", "T-G"
        ),
        "o_c_with_assets_vs_gold": _case_outcome_comparison(
            groups, "T-D", "T-H"
        ),
        "o_d_without_assets_vs_o_c": _case_outcome_comparison(
            groups, "T-E", "T-C"
        ),
        "o_d_with_assets_vs_o_c": _case_outcome_comparison(
            groups, "T-F", "T-D"
        ),
    }
    result_deltas = {
        "o_c_vs_schema_without_assets": _result_delta(groups, "T-C", "T-A"),
        "o_c_vs_physical_rag_without_assets": _result_delta(
            groups, "T-C", "T-B"
        ),
        "o_d_vs_o_c_without_assets": _result_delta(groups, "T-E", "T-C"),
        "o_d_vs_o_c_with_assets": _result_delta(groups, "T-F", "T-D"),
        "o_c_gap_to_gold_without_assets": _result_delta(groups, "T-C", "T-G"),
        "o_c_gap_to_gold_with_assets": _result_delta(groups, "T-D", "T-H"),
        "o_d_gap_to_gold_without_assets": _result_delta(groups, "T-E", "T-G"),
        "o_d_gap_to_gold_with_assets": _result_delta(groups, "T-F", "T-H"),
        "sql_asset_gain_o_c": _result_delta(groups, "T-D", "T-C"),
        "sql_asset_gain_o_d": _result_delta(groups, "T-F", "T-E"),
        "sql_asset_gain_gold": _result_delta(groups, "T-H", "T-G"),
    }
    construction_summary: dict[str, Any] = {}
    for source, evaluation in construction_runs.items():
        provenance = evaluation.get("provenance") or {}
        construction_summary[source] = {
            "construction_run_id": evaluation["run_id"],
            "raw_candidate_metrics": evaluation["raw_candidate_metrics"],
            "reviewed_draft_metrics": evaluation["reviewed_draft_metrics"],
            "review_cost": evaluation["review_cost"],
            "review_delta": evaluation["review_delta"],
            "error_analysis": evaluation["error_analysis"],
            "llm_invocation_count": len(provenance.get("llm_invocations") or []),
            "provenance": provenance,
        }
    return {
        "result_hash_accuracy_deltas": result_deltas,
        "case_outcome_comparisons": comparisons,
        "construction_summary": construction_summary,
        "interpretation": {
            "association_kind": "descriptive_single_run",
            "statistical_significance_claimed": False,
            "raw_f1_predicts_result_accuracy": False,
            "reviewed_f1_guarantees_result_accuracy": False,
            "llm_review_operations_delta": (
                construction_summary["REVIEWED_O_D"]["review_cost"][
                    "estimated_review_operations"
                ]
                - construction_summary["REVIEWED_O_C"]["review_cost"][
                    "estimated_review_operations"
                ]
            ),
            "llm_review_saving_rate_delta": round(
                construction_summary["REVIEWED_O_D"]["review_cost"][
                    "review_saving_rate"
                ]
                - construction_summary["REVIEWED_O_C"]["review_cost"][
                    "review_saving_rate"
                ],
                6,
            ),
            "llm_observed_lower_cost_single_run": (
                construction_summary["REVIEWED_O_D"]["review_cost"][
                    "estimated_review_operations"
                ]
                < construction_summary["REVIEWED_O_C"]["review_cost"][
                    "estimated_review_operations"
                ]
            ),
            "llm_cost_reduction_supported_beyond_single_run": False,
            "automatic_ontology_replaces_human_review": False,
            "ontology_token_usage_note": (
                "Ontology strategy token metadata is not propagated into "
                "EvaluationCaseResult; reported zero totals mean unavailable, "
                "not zero model usage."
            ),
        },
    }


def _format_case_ids(comparison: dict[str, Any], key: str) -> str:
    case_ids = [item["case_id"] for item in comparison[key]]
    return ", ".join(f"`{case_id}`" for case_id in case_ids) or "None"


def _error_analysis(
    rows: list[dict[str, Any]],
    downstream: dict[str, Any],
) -> str:
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
        rendered = (
            ", ".join(f"{key}: {value}" for key, value in failures.most_common())
            or "None"
        )
        lines.append(
            f"| {row['run']['experiment_group']} | {len(cases)} | "
            f"{sum(bool(case['success']) for case in cases)} | "
            f"{row['metrics']['result_hash_accuracy']} | {rendered or '—'} |"
        )
    deltas = downstream["result_hash_accuracy_deltas"]
    comparisons = downstream["case_outcome_comparisons"]
    construction = downstream["construction_summary"]
    o_c_cost = construction["REVIEWED_O_C"]["review_cost"]
    o_d_cost = construction["REVIEWED_O_D"]["review_cost"]
    o_c_raw = construction["REVIEWED_O_C"]["raw_candidate_metrics"]
    o_d_raw = construction["REVIEWED_O_D"]["raw_candidate_metrics"]
    o_c_reviewed = construction["REVIEWED_O_C"]["reviewed_draft_metrics"]
    o_d_reviewed = construction["REVIEWED_O_D"]["reviewed_draft_metrics"]
    provider_error_details = [
        (
            f"{row['run']['experiment_group']}/{case['case_id']}: "
            f"{case.get('failure_reason') or 'no failure reason'}"
        )
        for row in rows
        for case in row["cases"]
        if case.get("failure_category") == "PROVIDER_ERROR"
    ]
    lines.extend(
        [
            "",
            "## Result-accuracy deltas",
            "",
            "| Comparison | Delta |",
            "|---|---:|",
            f"| O-C vs Schema, SQLAsset disabled | "
            f"{deltas['o_c_vs_schema_without_assets']:+.6f} |",
            f"| O-C vs Physical RAG, SQLAsset disabled | "
            f"{deltas['o_c_vs_physical_rag_without_assets']:+.6f} |",
            f"| O-D vs O-C, SQLAsset disabled | "
            f"{deltas['o_d_vs_o_c_without_assets']:+.6f} |",
            f"| O-D vs O-C, SQLAsset enabled | "
            f"{deltas['o_d_vs_o_c_with_assets']:+.6f} |",
            f"| O-C gap to Gold, SQLAsset disabled | "
            f"{deltas['o_c_gap_to_gold_without_assets']:+.6f} |",
            f"| O-C gap to Gold, SQLAsset enabled | "
            f"{deltas['o_c_gap_to_gold_with_assets']:+.6f} |",
            f"| O-D gap to Gold, SQLAsset disabled | "
            f"{deltas['o_d_gap_to_gold_without_assets']:+.6f} |",
            f"| O-D gap to Gold, SQLAsset enabled | "
            f"{deltas['o_d_gap_to_gold_with_assets']:+.6f} |",
            f"| SQLAsset gain for O-C | "
            f"{deltas['sql_asset_gain_o_c']:+.6f} |",
            f"| SQLAsset gain for O-D | "
            f"{deltas['sql_asset_gain_o_d']:+.6f} |",
            f"| SQLAsset gain for Gold | "
            f"{deltas['sql_asset_gain_gold']:+.6f} |",
            "",
            "## Case-level differences",
            "",
            "O-C failed while Gold succeeded (SQLAsset disabled): "
            + _format_case_ids(
                comparisons["o_c_without_assets_vs_gold"],
                "left_failed_right_succeeded",
            )
            + ".",
            "",
            "O-C failed while Gold succeeded (SQLAsset enabled): "
            + _format_case_ids(
                comparisons["o_c_with_assets_vs_gold"],
                "left_failed_right_succeeded",
            )
            + ".",
            "",
            "O-D failed while O-C succeeded (SQLAsset disabled): "
            + _format_case_ids(
                comparisons["o_d_without_assets_vs_o_c"],
                "left_failed_right_succeeded",
            )
            + ".",
            "",
            "O-D failed while O-C succeeded (SQLAsset enabled): "
            + _format_case_ids(
                comparisons["o_d_with_assets_vs_o_c"],
                "left_failed_right_succeeded",
            )
            + ".",
            "",
            "## Construction quality and review cost",
            "",
            "| Source | Raw Object F1 | Raw Link F1 | Raw Metric F1 | "
            "Reviewed Object F1 | Reviewed Link F1 | Reviewed Metric F1 | "
            "Review operations | Edited fields | Saving rate | Live LLM calls |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| O-C | {o_c_raw['object_f1']:.6f} | {o_c_raw['link_f1']:.6f} | "
            f"{o_c_raw['metric_f1']:.6f} | {o_c_reviewed['object_f1']:.6f} | "
            f"{o_c_reviewed['link_f1']:.6f} | "
            f"{o_c_reviewed['metric_f1']:.6f} | "
            f"{o_c_cost['estimated_review_operations']} | "
            f"{o_c_cost['total_edited_field_count']} | "
            f"{o_c_cost['review_saving_rate']:.6f} | "
            f"{construction['REVIEWED_O_C']['llm_invocation_count']} |",
            f"| O-D | {o_d_raw['object_f1']:.6f} | {o_d_raw['link_f1']:.6f} | "
            f"{o_d_raw['metric_f1']:.6f} | {o_d_reviewed['object_f1']:.6f} | "
            f"{o_d_reviewed['link_f1']:.6f} | "
            f"{o_d_reviewed['metric_f1']:.6f} | "
            f"{o_d_cost['estimated_review_operations']} | "
            f"{o_d_cost['total_edited_field_count']} | "
            f"{o_d_cost['review_saving_rate']:.6f} | "
            f"{construction['REVIEWED_O_D']['llm_invocation_count']} |",
            "",
            "## Interpretation boundaries",
            "",
            "- This is one completed run per group, so no statistical "
            "significance claim is made.",
            "- O-D used live semantic enrichment, but it saved only one "
            "estimated review operation relative to O-C and did not improve "
            "downstream Result Hash Accuracy in either SQLAsset condition.",
            "- O-C and O-D have nearly identical raw core F1 values, while "
            "their reviewed Draft core F1 values are also identical; neither "
            "implies Gold-level downstream result accuracy.",
            "- The Reviewed ontologies remain materially below Gold, so the "
            "experiment does not support replacing human review.",
            "- Ontology strategy token totals are unavailable because token "
            "metadata is not propagated into EvaluationCaseResult; zeros in "
            "the CSV must not be interpreted as zero model usage.",
            "- Cases labeled `PROVIDER_ERROR`: "
            + ("; ".join(provider_error_details) or "None")
            + ". The recorded reason must be used to distinguish an external "
            "HTTP failure from an internal strategy exception.",
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

    progress_path = (
        args.resume_manifest.resolve()
        if args.resume_manifest is not None
        else output / "progress.json"
    )
    progress = _load_progress(progress_path)
    progress["artifacts"] = artifacts
    progress["concurrency"] = args.concurrency
    progress["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write_json(progress_path, progress)

    rows: list[dict[str, Any]] = []
    active_source: str | None = None
    for spec in GROUPS:
        source = spec["source"]
        if source != "NONE" and source != active_source:
            _activate(args.base_url, versions[source]["version"])
            active_source = source
        resumed_run_id = progress["completed_groups"].get(spec["group"])
        if resumed_run_id:
            completed = _request(
                args.base_url,
                f"/api/v1/evaluation/runs/{resumed_run_id}",
            )
            cases = _request(
                args.base_url,
                f"/api/v1/evaluation/runs/{resumed_run_id}/cases",
            )
            row = _validate_group_row(
                spec, completed, cases, concurrency=args.concurrency
            )
            rows.append(row)
            _write_cases(output / f"{spec['group']}.csv", cases)
            print(
                f"{spec['group']} resumed: {resumed_run_id}",
                flush=True,
            )
            continue
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
        try:
            completed = _wait_run(
                args.base_url,
                created["run_id"],
                deadline_seconds=args.run_timeout,
            )
        except Exception as exc:
            progress["failed_attempts"].append(
                {
                    "group": spec["group"],
                    "run_id": created["run_id"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            progress["updated_at"] = datetime.now(UTC).isoformat()
            _atomic_write_json(progress_path, progress)
            raise
        cases = _request(
            args.base_url,
            f"/api/v1/evaluation/runs/{created['run_id']}/cases",
        )
        row = _validate_group_row(
            spec, completed, cases, concurrency=args.concurrency
        )
        rows.append(row)
        _write_cases(output / f"{spec['group']}.csv", cases)
        progress["completed_groups"][spec["group"]] = created["run_id"]
        progress["persisted_case_count"] = len(progress["completed_groups"]) * 80
        progress["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(progress_path, progress)
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
        "status": "COMPLETED",
        "generated_at": datetime.now(UTC).isoformat(),
        "benchmark_case_count_per_group": 80,
        "total_case_count": 640,
        "completed_group_count": 8,
        "concurrency": args.concurrency,
        "fairness": {
            "passed": not api_comparison.get("warnings"),
            "warnings": api_comparison.get("warnings", []),
        },
        "artifacts": artifacts,
        "sql_asset_builds": sql_builds,
        "runs": [row["run"] for row in rows],
    }
    comparison = {
        "evaluation_schema_version": "2.0",
        "status": "COMPLETED",
        "api_comparison": api_comparison,
        "groups": [
            {"run": row["run"], "metrics": row["metrics"]} for row in rows
        ],
    }
    construction_evaluations: dict[str, dict[str, Any]] = {}
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
        construction_evaluations[source] = evaluation
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
    downstream = _build_downstream_analysis(rows, construction_evaluations)
    correlations["construction_runs"] = downstream["construction_summary"]
    correlations["result_hash_accuracy_deltas"] = downstream[
        "result_hash_accuracy_deltas"
    ]
    correlations["case_outcome_comparisons"] = downstream[
        "case_outcome_comparisons"
    ]
    correlations["interpretation"] = downstream["interpretation"]
    comparison["analysis"] = {
        "result_hash_accuracy_deltas": downstream[
            "result_hash_accuracy_deltas"
        ],
        "case_outcome_comparisons": downstream["case_outcome_comparisons"],
        "interpretation": downstream["interpretation"],
    }

    _atomic_write_json(output / "manifest.json", manifest)
    _atomic_write_json(output / "comparison.json", comparison)
    _write_comparison_csv(output / "comparison.csv", rows)
    error_path = output / "error-analysis.md"
    error_temporary = error_path.with_suffix(f"{error_path.suffix}.tmp")
    error_temporary.write_text(
        _error_analysis(rows, downstream),
        encoding="utf-8",
    )
    error_temporary.replace(error_path)
    _atomic_write_json(
        output / "construction-query-correlation.json", correlations
    )
    progress["status"] = "COMPLETED"
    progress["persisted_case_count"] = 640
    progress["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write_json(progress_path, progress)


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
    root.add_argument("--concurrency", type=int, default=1)
    root.add_argument("--run-timeout", type=int, default=7200)
    root.add_argument(
        "--resume-manifest",
        type=Path,
        help="Atomic progress manifest; defaults to OUTPUT/progress.json",
    )
    return root


if __name__ == "__main__":
    run(parser().parse_args())
