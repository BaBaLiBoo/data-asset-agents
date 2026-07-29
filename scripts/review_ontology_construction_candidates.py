"""Post-generation Gold-backed reviewer used only by local acceptance tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from data_asset_agents.evaluation.ontology_construction import GoldOntologyLoader

GROUPS = {
    "object_type": "object_types",
    "property": "properties",
    "binding": "bindings",
    "link_type": "link_types",
    "physical_join": "physical_joins",
    "dimension": "dimensions",
    "metric": "metrics",
}
ALLOWED_OBJECTS = {"customer", "account", "card", "transaction", "branch", "merchant"}
ALLOWED_TABLES = {
    "dim_customer",
    "dim_account",
    "dim_card",
    "dwd_card_transaction",
    "dim_branch",
    "dim_merchant",
}
FINAL_STATUSES = {"ACCEPTED", "MODIFIED", "REJECTED", "MERGED"}
LLM_SEMANTIC_FIELDS = {
    "object_type": {"name", "plural_name", "description"},
    "property": {"name", "description", "synonyms"},
    "link_type": {"name", "inverse_name"},
    "dimension": {"name", "description", "synonyms"},
    "metric": {"name", "description"},
}


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(f"{base_url.rstrip('/')}{path}", body, headers, method=method)
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail}") from exc


def _safe_unmatched_candidate(resource_type: str, resource: dict[str, Any]) -> bool:
    if resource_type == "object_type":
        return resource["id"] in ALLOWED_OBJECTS
    if resource_type in {"property", "binding"}:
        return resource["object_type_id"] in ALLOWED_OBJECTS
    if resource_type == "link_type":
        return {
            resource["source_object_type_id"],
            resource["target_object_type_id"],
        } <= ALLOWED_OBJECTS
    if resource_type == "physical_join":
        return {resource["left_table"], resource["right_table"]} <= ALLOWED_TABLES
    if resource_type == "dimension":
        return resource["property_id"].split(".", 1)[0] in ALLOWED_OBJECTS
    return False


def _reviewed_resource(
    resource_type: str,
    candidate: dict[str, Any],
    gold: dict[str, Any],
    *,
    retain_llm_semantics: bool,
    retain_object_descriptions_only: bool,
) -> dict[str, Any]:
    reviewed = dict(gold)
    retained_fields = (
        {"description"}
        if retain_object_descriptions_only and resource_type == "object_type"
        else (
            LLM_SEMANTIC_FIELDS.get(resource_type, set())
            if retain_llm_semantics
            else set()
        )
    )
    for field in retained_fields:
        value = candidate.get(field)
        if value not in (None, "", []):
            reviewed[field] = value
    if resource_type == "binding":
        reviewed = {
            **reviewed,
            "latest_snapshot_id": candidate.get("latest_snapshot_id"),
            "schema_columns": candidate.get("schema_columns", []),
        }
    return reviewed


def review(
    base_url: str,
    run_id: str,
    gold_path: Path,
    *,
    retain_llm_semantics: bool = False,
    retain_object_descriptions_only: bool = False,
) -> dict[str, Any]:
    if retain_llm_semantics and retain_object_descriptions_only:
        raise ValueError("Choose only one LLM semantic retention policy")
    run = _request(base_url, f"/api/v1/ontology/construction-runs/{run_id}")
    if run["status"] not in {"CANDIDATES_READY", "UNDER_REVIEW"}:
        raise RuntimeError(
            "Gold acceptance reviewer may run only after candidate generation; "
            f"run status is {run['status']}"
        )
    candidates = _request(
        base_url, f"/api/v1/ontology/construction-runs/{run_id}/candidates"
    )

    # Freeze and score the immutable raw snapshot before this reviewer loads Gold.
    raw_evaluation = _request(
        base_url,
        f"/api/v1/ontology/construction-runs/{run_id}/evaluation/raw",
    )
    # Gold is deliberately loaded only after generation and raw scoring completed.
    gold, gold_hash = GoldOntologyLoader(gold_path).load()
    gold_maps = {
        resource_type: {
            item.id: item.model_dump(mode="json")
            for item in getattr(gold, collection_name)
        }
        for resource_type, collection_name in GROUPS.items()
    }
    counts = {"ACCEPT": 0, "MODIFY": 0, "REJECT": 0, "SKIPPED_FINAL": 0}
    for candidate in candidates:
        if candidate["status"] in FINAL_STATUSES:
            counts["SKIPPED_FINAL"] += 1
            continue
        resource_type = candidate["resource_type"]
        resource = candidate["current_resource"]
        gold_resource = gold_maps[resource_type].get(resource["id"])
        if gold_resource is not None:
            decision = "MODIFY"
            modified_resource = _reviewed_resource(
                resource_type,
                resource,
                gold_resource,
                retain_llm_semantics=retain_llm_semantics,
                retain_object_descriptions_only=retain_object_descriptions_only,
            )
            comment = (
                "Post-generation Gold acceptance review; stable resource ID preserved"
                + (
                    "; allowed live LLM semantic suggestions retained by reviewer"
                    if retain_llm_semantics or retain_object_descriptions_only
                    else ""
                )
            )
        elif _safe_unmatched_candidate(resource_type, resource):
            decision = "ACCEPT"
            modified_resource = None
            comment = "Post-generation structural acceptance review"
        else:
            decision = "REJECT"
            modified_resource = None
            comment = "Outside fictional MiniBank Gold acceptance scope"
        _request(
            base_url,
            (
                f"/api/v1/ontology/construction-runs/{run_id}/candidates/"
                f"{candidate['candidate_id']}/review"
            ),
            method="POST",
            payload={
                "decision": decision,
                "reviewer": "local-acceptance-test-reviewer",
                "comment": comment,
                "modified_resource": modified_resource,
            },
        )
        counts[decision] += 1
    return {
        "run_id": run_id,
        "reviewer_kind": "POST_GENERATION_GOLD_TEST_REVIEWER",
        "review_policy": (
            "GOLD_STRUCTURE_WITH_REVIEWED_LLM_SEMANTICS"
            if retain_llm_semantics
            else (
                "GOLD_ALIGNED_WITH_REVIEWED_LLM_OBJECT_DESCRIPTIONS"
                if retain_object_descriptions_only
                else "GOLD_ALIGNED"
            )
        ),
        "gold_hash": gold_hash,
        "gold_loaded_after_generation": True,
        "raw_evaluated_before_gold_review": (
            raw_evaluation.get("provenance", {}).get("evaluation_stage")
            == "RAW_CANDIDATE"
        ),
        "decisions": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--gold-path",
        type=Path,
        default=Path("ontology/retail_banking/object_model"),
    )
    parser.add_argument(
        "--retain-llm-semantics",
        action="store_true",
        help=(
            "Retain only allow-listed O-D semantic suggestions while Gold remains "
            "authoritative for structural and physical fields."
        ),
    )
    parser.add_argument(
        "--retain-object-descriptions-only",
        action="store_true",
        help=(
            "Retain only reviewed O-D object boundary descriptions. This is the "
            "query-safe formal profile after names/synonyms fail strict Dry Run."
        ),
    )
    args = parser.parse_args()
    print(
        json.dumps(
            review(
                args.base_url,
                args.run_id,
                args.gold_path,
                retain_llm_semantics=args.retain_llm_semantics,
                retain_object_descriptions_only=args.retain_object_descriptions_only,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
