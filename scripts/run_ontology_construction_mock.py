"""Reproduce O-A..O-D construction metrics from public MiniBank files only."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.evaluation.ontology_construction import (
    GoldOntologyLoader,
    OntologyConstructionEvaluator,
)
from data_asset_agents.metadata.inspector import mask_sample_value
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.governed_catalog import load_governed_catalog
from data_asset_agents.ontology.manager.models import (
    CatalogMode,
    ConstructionCandidate,
    ConstructionCandidateStatus,
    ConstructionEvidenceMode,
    ConstructionMode,
    DraftResources,
    LifecycleStatus,
    OntologyConstructionRun,
)
from data_asset_agents.ontology.models import (
    ColumnMetadata,
    ColumnProfile,
    ForeignKeyMetadata,
    IndexMetadata,
    MetadataSnapshot,
    TableMetadata,
    ValueFrequency,
)
from data_asset_agents.sql_assets import HistoricalSQLParser

ROOT = Path(__file__).parents[1]
OUTPUT = ROOT / "reports/ontology_construction"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_hash(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _git_sha() -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _literal(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal):
        return str(node.this)
    return node.sql(dialect="postgres")


def _seed_rows() -> dict[str, dict[str, list[str | None]]]:
    result: dict[str, dict[str, list[str | None]]] = {}
    statements = sqlglot.parse(
        (ROOT / "data/seed/002_seed.sql").read_text(encoding="utf-8"),
        read="postgres",
    )
    for statement in statements:
        if not isinstance(statement, exp.Insert) or not isinstance(
            statement.expression, exp.Values
        ):
            continue
        table = statement.this.this.name
        columns = [item.name for item in statement.this.expressions]
        target = result.setdefault(table, {column: [] for column in columns})
        for row in statement.expression.expressions:
            for column, value in zip(columns, row.expressions, strict=True):
                target[column].append(_literal(value))
    return result


def _profiles(
    table_name: str,
    columns: list[ColumnMetadata],
    rows: dict[str, dict[str, list[str | None]]],
) -> list[ColumnProfile]:
    table_rows = rows.get(table_name, {})
    result: list[ColumnProfile] = []
    for column in columns:
        values = table_rows.get(column.name, [])
        present = [value for value in values if value is not None]
        counts = Counter(present)
        result.append(
            ColumnProfile(
                table_name=table_name,
                column_name=column.name,
                data_type=column.data_type,
                row_count=len(values),
                null_count=len(values) - len(present),
                null_rate=(len(values) - len(present)) / (len(values) or 1),
                distinct_count=len(counts),
                unique_rate=len(counts) / (len(values) or 1),
                minimum=min(present) if present else None,
                maximum=max(present) if present else None,
                top_values=[
                    ValueFrequency(
                        value=mask_sample_value(column.name, value), count=count
                    )
                    for value, count in counts.most_common(5)
                ],
                sample_values=[
                    mask_sample_value(column.name, value)
                    for value in sorted(counts)[:5]
                ],
            )
        )
    return result


def build_snapshot() -> MetadataSnapshot:
    rows = _seed_rows()
    tables: list[TableMetadata] = []
    indexes: dict[str, list[IndexMetadata]] = {}
    statements = sqlglot.parse(
        (ROOT / "data/ddl/001_schema.sql").read_text(encoding="utf-8"),
        read="postgres",
    )
    for statement in statements:
        if isinstance(statement, exp.Create) and statement.args.get("kind") == "INDEX":
            target = next(statement.find_all(exp.Table), None)
            if target:
                indexes.setdefault(target.name, []).append(
                    IndexMetadata(
                        name=statement.this.name,
                        columns=[column.name for column in statement.find_all(exp.Column)],
                    )
                )
    for statement in statements:
        if not isinstance(statement, exp.Create) or statement.args.get("kind") != "TABLE":
            continue
        schema = statement.this
        if not isinstance(schema, exp.Schema):
            continue
        table_name = schema.this.name
        columns: list[ColumnMetadata] = []
        primary_key: list[str] = []
        foreign_keys: list[ForeignKeyMetadata] = []
        for definition in schema.expressions:
            if isinstance(definition, exp.ColumnDef):
                constraints = [
                    constraint.args.get("kind")
                    for constraint in definition.constraints
                ]
                columns.append(
                    ColumnMetadata(
                        name=definition.name,
                        data_type=definition.args["kind"].sql(dialect="postgres"),
                        nullable=not any(
                            isinstance(item, exp.NotNullColumnConstraint)
                            for item in constraints
                        ),
                    )
                )
                if any(
                    isinstance(item, exp.PrimaryKeyColumnConstraint)
                    for item in constraints
                ):
                    primary_key.append(definition.name)
                reference = next(
                    (item for item in constraints if isinstance(item, exp.Reference)),
                    None,
                )
                if reference is not None:
                    referred = reference.this
                    foreign_keys.append(
                        ForeignKeyMetadata(
                            constrained_columns=[definition.name],
                            referred_table=referred.this.name,
                            referred_columns=[item.name for item in referred.expressions],
                        )
                    )
            elif isinstance(definition, exp.PrimaryKey):
                primary_key.extend(item.name for item in definition.expressions)
        tables.append(
            TableMetadata(
                schema_name="public",
                table_name=table_name,
                columns=columns,
                primary_key=primary_key,
                foreign_keys=foreign_keys,
                indexes=indexes.get(table_name, []),
                profiles=_profiles(table_name, columns, rows),
            )
        )
    return MetadataSnapshot(
        id="mock-minibank-files-v1",
        schema_name="public",
        source="public-ddl-and-seed-files",
        tables=tables,
    )


def _candidate_resources(
    generated: Any, gold: DraftResources, run_id: str
) -> tuple[DraftResources, list[ConstructionCandidate]]:
    resources = DraftResources()
    reviews: list[ConstructionCandidate] = []
    gold_groups = {
        "object_type": ("object_types", "object_type"),
        "property": ("properties", "property"),
        "binding": ("bindings", "binding"),
        "link_type": ("link_types", "link_type"),
        "physical_join": ("physical_joins", "physical_join"),
        "dimension": ("dimensions", "dimension"),
        "metric": ("metrics", "metric"),
    }
    for resource_type, (collection, payload_name) in gold_groups.items():
        gold_by_id = {item.id: item for item in getattr(gold, collection)}
        for candidate in getattr(generated, collection):
            resource = getattr(candidate, payload_name)
            target = gold_by_id.get(resource.id)
            if target is None:
                status = ConstructionCandidateStatus.REJECTED
                current = resource
            else:
                comparable = resource.model_dump(
                    mode="json", exclude={"created_at", "updated_at", "lifecycle_status"}
                )
                gold_comparable = target.model_dump(
                    mode="json", exclude={"created_at", "updated_at", "lifecycle_status"}
                )
                if comparable == gold_comparable:
                    status = ConstructionCandidateStatus.ACCEPTED
                    current = resource
                else:
                    status = ConstructionCandidateStatus.MODIFIED
                    current = target
                getattr(resources, collection).append(
                    current.model_copy(
                        update={"lifecycle_status": LifecycleStatus.ACTIVE}
                    )
                    if hasattr(current, "lifecycle_status")
                    else current
                )
            reviews.append(
                ConstructionCandidate(
                    candidate_id=candidate.candidate_id,
                    run_id=run_id,
                    resource_type=resource_type,
                    status=status,
                    original_candidate=candidate.model_dump(mode="json"),
                    current_resource=current.model_dump(mode="json"),
                    evidence=candidate.evidence,
                    reviewer="deterministic-mock-review-policy",
                    decision=(
                        "ACCEPT"
                        if status == ConstructionCandidateStatus.ACCEPTED
                        else "MODIFY"
                        if status == ConstructionCandidateStatus.MODIFIED
                        else "REJECT"
                    ),
                    revision=1,
                )
            )
    return resources, reviews


def run() -> list[dict[str, Any]]:
    snapshot = build_snapshot()
    metadata_snapshot_hash = _payload_hash(snapshot.model_dump(mode="json"))
    profiling_snapshot_hash = _payload_hash(
        {
            table.table_name: [
                profile.model_dump(mode="json") for profile in table.profiles
            ]
            for table in snapshot.tables
        }
    )
    database_snapshot_hash = _payload_hash(
        {
            "ddl_sha256": _file_hash(ROOT / "data/ddl/001_schema.sql"),
            "seed_sha256": _file_hash(ROOT / "data/seed/002_seed.sql"),
        }
    )
    historical_sql_hash = _file_hash(ROOT / "data/historical_sql/examples.json")
    git_sha = _git_sha()
    historical_sql = HistoricalSQLParser().parse_file(
        ROOT / "data/historical_sql/examples.json"
    )
    gold, gold_hash = GoldOntologyLoader(
        ROOT / "ontology/retail_banking/object_model"
    ).load()
    catalog = load_governed_catalog(
        ROOT / "data/governed_catalog/minibank_tables.json"
    )
    experiments = [
        (mode, CatalogMode.RAW_METADATA)
        for mode in ConstructionEvidenceMode
    ] + [(ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL, CatalogMode.GOVERNED_CATALOG)]
    reports: list[dict[str, Any]] = []
    for mode, catalog_mode in experiments:
        include_profiles = mode != ConstructionEvidenceMode.O_A_SCHEMA_ONLY
        include_sql = mode in {
            ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL,
            ConstructionEvidenceMode.O_D_ALL_EVIDENCE_LLM,
        }
        evidence_snapshot = (
            snapshot
            if include_profiles
            else snapshot.model_copy(
                update={
                    "tables": [
                        table.model_copy(update={"profiles": []})
                        for table in snapshot.tables
                    ]
                }
            )
        )
        run_id = f"mock-{mode.value}-{catalog_mode.value.lower()}"
        generated = ObjectFirstCandidateGenerator(
            Settings(llm_mode="mock"), catalog
        ).generate(
            evidence_snapshot,
            historical_sql if include_sql else [],
            catalog_mode=catalog_mode,
        )
        resources, reviews = _candidate_resources(generated, gold, run_id)
        try:
            compilation = ObjectSemanticCompiler(
                fallback_bundle=None,
                construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
            ).compile(
                resources,
                construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
            )
            compilation_conflicts = compilation.conflicts
            seed_accessed = compilation.seed_accessed
            fallback_used = compilation.fallback_used
            legacy_ontology_accessed = compilation.legacy_ontology_accessed
        except OntologyError as exc:
            compilation_conflicts = [str(exc)]
            seed_accessed = False
            fallback_used = False
            legacy_ontology_accessed = False
        strict_validation_passed = not (
            compilation_conflicts
            or seed_accessed
            or fallback_used
            or legacy_ontology_accessed
        )
        construction_run = OntologyConstructionRun(
            run_id=run_id,
            status="EVALUATED",
            source_snapshot_id=snapshot.id,
            source_snapshot_hash=metadata_snapshot_hash,
            profiling_snapshot_hash=profiling_snapshot_hash,
            historical_sql_snapshot_hash=historical_sql_hash,
            catalog_mode=catalog_mode,
            construction_mode=ConstructionMode.STRICT_CONSTRUCTION,
            evidence_mode=mode,
            llm_mode="mock",
            provider="mock",
            model="deterministic-rules-v1",
            temperature=0,
            random_seed=20260715,
            git_sha=git_sha,
            created_by="mock-report-script",
            strict_validation_passed=strict_validation_passed,
            seed_accessed=seed_accessed,
            fallback_used=fallback_used,
            legacy_ontology_accessed=legacy_ontology_accessed,
        )
        report = OntologyConstructionEvaluator().evaluate(
            construction_run, resources, reviews, gold, gold_hash
        )
        payload = report.model_dump(mode="json")
        payload["candidate_counts"] = {
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
        payload["excluded_tables"] = generated.excluded_tables
        payload["decision_counts"] = dict(
            Counter(candidate.decision for candidate in reviews)
        )
        payload["strict_compilation_conflicts"] = compilation_conflicts
        payload["provenance"]["database_snapshot_hash"] = database_snapshot_hash
        payload["provenance"]["metadata_snapshot_hash"] = metadata_snapshot_hash
        payload["provenance"]["gold_hash"] = gold_hash
        payload["provenance"]["result_mode"] = (
            "mock deterministic construction; no live LLM claim"
        )
        reports.append(payload)
    return reports


def write_reports(reports: list[dict[str, Any]]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "local_mock_ablation.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metric_names = sorted(
        {name for report in reports for name in report["metrics"]}
    )
    with (OUTPUT / "local_mock_ablation.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["run_id", "evidence_mode", "catalog_mode", *metric_names],
            lineterminator="\n",
        )
        writer.writeheader()
        for report in reports:
            writer.writerow(
                {
                    "run_id": report["run_id"],
                    "evidence_mode": report["provenance"]["evidence_mode"],
                    "catalog_mode": report["provenance"]["catalog_mode"],
                    **report["metrics"],
                }
            )
    lines = [
        "# Ontology Construction Mock Ablation",
        "",
        "本报告由公开 MiniBank DDL、虚构 seed 和认证历史 SQL 确定性生成；"
        "Gold 仅在候选生成完成后用于模拟审核和评分。",
        "",
        "| Run | Catalog | Object F1 | Dimension F1 | Metric F1 | 接受率 | 修改率 | 拒绝率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        metrics = report["metrics"]
        lines.append(
            f"| {report['run_id']} | {report['provenance']['catalog_mode']} | "
            f"{metrics['object_f1']:.3f} | {metrics['dimension_f1']:.3f} | "
            f"{metrics['metric_f1']:.3f} | {metrics['direct_acceptance_rate']:.3f} | "
            f"{metrics['modification_rate']:.3f} | {metrics['rejection_rate']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- O-A 只验证 schema 对对象、属性和绑定骨架的贡献。",
            "- O-B 加入画像，当前规则主要改善枚举/敏感性建议；不会凭画像创造业务指标。",
            "- O-C 的认证 SQL AST 才能提出聚合、固定过滤、时间字段和支持维度。",
            "- O-D 在本报告中使用 mock 确定性语义增强，因此不声称存在 live LLM 增益。",
            "- 对象边界、Link 业务命名、冲突过滤和指标口径仍需人工确认。",
        ]
    )
    lines = [
        "# Ontology Construction Mock Ablation",
        "",
        "本报告由公开 MiniBank DDL、虚构 seed 和认证历史 SQL 确定性生成；"
        "Gold 仅在候选生成完成后用于模拟审核和评分。",
        "",
        "| Run | Catalog | Object F1 | Dimension F1 | Metric F1 | 接受率 | 修改率 | 拒绝率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        metrics = report["metrics"]
        lines.append(
            f"| {report['run_id']} | {report['provenance']['catalog_mode']} | "
            f"{metrics['object_f1']:.3f} | {metrics['dimension_f1']:.3f} | "
            f"{metrics['metric_f1']:.3f} | {metrics['direct_acceptance_rate']:.3f} | "
            f"{metrics['modification_rate']:.3f} | {metrics['rejection_rate']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- O-A 只验证 schema 对对象、属性和绑定骨架的贡献。",
            "- O-B 加入画像，当前规则主要改善枚举/敏感性建议；不会凭画像创造业务指标。",
            "- O-C 的认证 SQL AST 才能提出聚合、固定过滤、时间字段和支持维度。",
            "- O-D 在本报告中使用 mock 确定性语义增强，因此不声称存在 live LLM 增益。",
            "- 对象边界、Link 业务命名、冲突过滤和指标口径仍需人工确认。",
        ]
    )
    lines = [
        "# Ontology Construction Local Mock Ablation",
        "",
        "Generated deterministically from the public MiniBank DDL, fictional seed "
        "data, and certified historical SQL. Gold is loaded only after candidate "
        "generation to simulate human review and independent scoring.",
        "",
        "| Run | Catalog | Object F1 | Dimension F1 | Metric F1 | Accept | Modify | Reject |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        metrics = report["metrics"]
        lines.append(
            f"| {report['run_id']} | {report['provenance']['catalog_mode']} | "
            f"{metrics['object_f1']:.3f} | {metrics['dimension_f1']:.3f} | "
            f"{metrics['metric_f1']:.3f} | {metrics['direct_acceptance_rate']:.3f} | "
            f"{metrics['modification_rate']:.3f} | {metrics['rejection_rate']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- O-A measures the schema-only object, property, and binding scaffold.",
            "- O-B adds profiling; profiling does not invent business metrics.",
            "- O-C may derive aggregation, fixed filters, time properties, and "
            "supported dimensions only from certified SQL AST evidence.",
            "- O-D uses mock semantic enrichment and makes no live-LLM gain claim.",
            "- Object boundaries, business Link names, conflicts, and metric "
            "definitions still require human review.",
            "",
            "## Provenance and validation",
            "",
            f"- Git SHA: `{reports[0]['provenance']['git_sha']}`",
            f"- Database snapshot hash: "
            f"`{reports[0]['provenance']['database_snapshot_hash']}`",
            f"- Metadata snapshot hash: "
            f"`{reports[0]['provenance']['metadata_snapshot_hash']}`",
            f"- Historical SQL hash: "
            f"`{reports[0]['provenance']['historical_sql_snapshot_hash']}`",
            f"- Gold hash: `{reports[0]['gold_hash']}`",
            "- `strict_validation_passed` in this report means deterministic strict "
            "compilation completed without conflicts or seed/fallback/legacy access. "
            "It is not a PostgreSQL EXPLAIN or live-model result.",
            "- Publication and runtime activation are intentionally false in this "
            "file-only ablation; those gates are covered by Docker acceptance.",
        ]
    )
    (OUTPUT / "local_mock_ablation.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    write_reports(run())
