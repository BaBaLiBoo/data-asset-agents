"""Run isolated O-C versus live O-D semantic construction evaluation."""

from __future__ import annotations

import csv
import json
from typing import Any

from run_ontology_construction_mock import (
    ROOT,
    _candidate_resources,
    _file_hash,
    _git_sha,
    _payload_hash,
    build_snapshot,
)

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.evaluation.ontology_construction import (
    GoldOntologyLoader,
    OntologyConstructionEvaluator,
)
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.construction import OntologyConstructionService
from data_asset_agents.ontology.manager.governed_catalog import load_governed_catalog
from data_asset_agents.ontology.manager.models import (
    CatalogMode,
    ConstructionEvidenceMode,
    ConstructionMode,
    OntologyConstructionRun,
)
from data_asset_agents.sql_assets import HistoricalSQLParser

OUTPUT = ROOT / "reports/ontology_construction_v2"


def run() -> list[dict[str, Any]]:
    settings = Settings()
    key = settings.llm_api_key.get_secret_value().strip()
    if not key:
        raise OntologyError("LLM_API_KEY is not configured")
    snapshot = build_snapshot()
    metadata_hash = _payload_hash(snapshot.model_dump(mode="json"))
    profile_hash = _payload_hash(
        {
            table.table_name: [
                profile.model_dump(mode="json") for profile in table.profiles
            ]
            for table in snapshot.tables
        }
    )
    sql_path = ROOT / "data/historical_sql/examples.json"
    historical_sql = HistoricalSQLParser().parse_file(sql_path)
    historical_sql_hash = _file_hash(sql_path)
    gold_loader = GoldOntologyLoader(ROOT / "ontology/retail_banking/object_model")
    catalog = load_governed_catalog(
        ROOT / "data/governed_catalog/minibank_tables.json"
    )
    reports: list[dict[str, Any]] = []
    for catalog_mode in CatalogMode:
        for mode, llm_mode in (
            (ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL, "mock"),
            (ConstructionEvidenceMode.O_D_ALL_EVIDENCE_LLM, "live"),
        ):
            run_id = f"live-{mode.value}-{catalog_mode.value.lower()}"
            generator_settings = settings.model_copy(update={"llm_mode": llm_mode})
            generated = ObjectFirstCandidateGenerator(
                generator_settings, catalog
            ).generate(
                snapshot,
                historical_sql,
                catalog_mode=catalog_mode,
            )
            gold, gold_hash = gold_loader.load()
            construction_run = OntologyConstructionRun(
                run_id=run_id,
                status="EVALUATED",
                source_snapshot_id=snapshot.id,
                source_snapshot_hash=metadata_hash,
                profiling_snapshot_hash=profile_hash,
                historical_sql_snapshot_hash=historical_sql_hash,
                catalog_mode=catalog_mode,
                construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
                evidence_mode=mode,
                llm_mode=llm_mode,
                provider=settings.llm_provider if llm_mode == "live" else "none",
                model=settings.llm_model if llm_mode == "live" else "deterministic-rules-v1",
                temperature=0,
                random_seed=20260727,
                git_sha=_git_sha(),
                created_by="live-quality-v2-script",
                llm_invocations=[
                    {**item, "random_seed": 20260727}
                    for item in generated.llm_invocations
                ],
            )
            candidates = OntologyConstructionService._flatten(run_id, generated)
            evaluator = OntologyConstructionEvaluator()
            raw = evaluator.evaluate_raw(
                construction_run, candidates, gold, gold_hash
            )
            reviewed_resources, reviews = _candidate_resources(
                generated, gold, run_id
            )
            try:
                compiled = ObjectSemanticCompiler(
                    fallback_bundle=None,
                    construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
                ).compile(
                    reviewed_resources,
                    construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
                )
                conflicts = compiled.conflicts
                construction_run.seed_accessed = compiled.seed_accessed
                construction_run.fallback_used = compiled.fallback_used
                construction_run.legacy_ontology_accessed = (
                    compiled.legacy_ontology_accessed
                )
            except OntologyError as exc:
                conflicts = [str(exc)]
            construction_run.strict_validation_passed = not (
                conflicts
                or construction_run.seed_accessed
                or construction_run.fallback_used
                or construction_run.legacy_ontology_accessed
            )
            report = evaluator.evaluate(
                construction_run,
                reviewed_resources,
                reviews,
                gold,
                gold_hash,
                raw_report=raw,
            ).model_dump(mode="json")
            report["candidate_counts"] = {
                name: len(getattr(generated, name))
                for name in (
                    "object_types",
                    "properties",
                    "bindings",
                    "link_types",
                    "physical_joins",
                    "dimensions",
                    "metrics",
                )
            }
            report["strict_compilation_conflicts"] = conflicts
            report["provenance"]["metadata_snapshot_hash"] = metadata_hash
            report["provenance"]["gold_hash"] = gold_hash
            report["provenance"]["result_mode"] = (
                "live semantic enrichment" if llm_mode == "live" else "no LLM"
            )
            reports.append(report)
    return reports


def write(reports: list[dict[str, Any]]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "live_ablation_v2.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = [
        "run_id",
        "catalog_mode",
        "evidence_mode",
        "llm_mode",
        "raw_object_f1",
        "raw_endpoint_link_f1",
        "raw_directed_link_f1",
        "raw_semantic_link_f1",
        "raw_metric_f1",
        "edited_fields",
        "saving_rate",
        "llm_calls",
        "strict_validation_passed",
    ]
    with (OUTPUT / "live_ablation_v2.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for report in reports:
            raw = report["raw_candidate_metrics"]
            cost = report["review_cost"]
            provenance = report["provenance"]
            writer.writerow(
                {
                    "run_id": report["run_id"],
                    "catalog_mode": provenance["catalog_mode"],
                    "evidence_mode": provenance["evidence_mode"],
                    "llm_mode": provenance["llm_mode"],
                    "raw_object_f1": raw.get("object_f1"),
                    "raw_endpoint_link_f1": raw.get("link_endpoint_pair_f1"),
                    "raw_directed_link_f1": raw.get("directed_link_f1"),
                    "raw_semantic_link_f1": raw.get("semantic_link_f1"),
                    "raw_metric_f1": raw.get("metric_f1"),
                    "edited_fields": cost.get("total_edited_field_count"),
                    "saving_rate": cost.get("review_saving_rate"),
                    "llm_calls": len(provenance.get("llm_invocations", [])),
                    "strict_validation_passed": report[
                        "reviewed_draft_metrics"
                    ].get("strict_validation_passed"),
                }
            )
    lines = [
        "# O-C versus Live O-D Construction Ablation V2",
        "",
        "| Run | Catalog | Raw Object F1 | Endpoint Link F1 | Directed Link F1 | "
        "Semantic Link F1 | Raw Metric F1 | Edited Fields | Saving Rate | LLM Calls |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        raw = report["raw_candidate_metrics"]
        cost = report["review_cost"]
        provenance = report["provenance"]
        lines.append(
            f"| {report['run_id']} | {provenance['catalog_mode']} | "
            f"{raw.get('object_f1')} | {raw.get('link_endpoint_pair_f1')} | "
            f"{raw.get('directed_link_f1')} | {raw.get('semantic_link_f1')} | "
            f"{raw.get('metric_f1')} | {cost.get('total_edited_field_count')} | "
            f"{cost.get('review_saving_rate')} | "
            f"{len(provenance.get('llm_invocations', []))} |"
        )
    lines.extend(
        [
            "",
            "O-C uses no semantic model. O-D uses the same physical evidence plus "
            "constrained live semantic suggestions. These file-only runs do not claim "
            "publication, runtime activation, or downstream 80-case results.",
        ]
    )
    (OUTPUT / "live_ablation_v2.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    write(run())
