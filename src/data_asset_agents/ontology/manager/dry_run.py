"""Dynamic semantic Dry Run for an object-model Draft."""

from __future__ import annotations

import sqlglot

from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.graph import build_text2sql_graph

from .compiler import ObjectSemanticCompiler
from .governance_models import DraftSemanticDryRunCase
from .models import ConstructionMode, DraftResources

CORE_CASES = (
    ("core-branch-card", "查询近30天各分行信用卡交易金额和交易笔数。"),
    ("core-branch-rank", "查询近30天各分行信用卡交易金额和排名。"),
    ("core-channel-amount", "按交易渠道查询交易金额。"),
    ("core-branch-active-customer", "查询各分行活跃客户数。"),
)


class DraftSemanticDryRun:
    """Compile each core business question through the candidate object bundle."""

    def __init__(self, executor: QueryExecutor) -> None:
        self.executor = executor

    def run(
        self,
        resources: DraftResources,
        fallback: OntologyBundle | None,
        *,
        construction_mode: ConstructionMode = ConstructionMode.LEGACY_COMPAT,
    ) -> list[DraftSemanticDryRunCase]:
        compilation = ObjectSemanticCompiler(
            fallback, construction_mode=construction_mode
        ).compile(resources)
        repository = (
            _FixedBundleRepository(compilation.bundle)
            if construction_mode == ConstructionMode.STRICT_CONSTRUCTION
            else YamlOntologyRepository(self.executor.settings.ontology_path)
        )
        ontology = OntologyService(
            repository,  # type: ignore[arg-type]
            settings=self.executor.settings,
            bundle_override=compilation.bundle,
        )
        graph = build_text2sql_graph(
            ontology, self.executor, sql_assets=None, history_enabled=False
        )
        reports: list[DraftSemanticDryRunCase] = []
        for case_id, question in CORE_CASES:
            report = DraftSemanticDryRunCase(
                benchmark_case_id=case_id,
                question=question,
                semantic_property_resolution={
                    metric.id: metric.measure_property_id or "legacy"
                    for metric in compilation.bundle.metrics
                    if metric.name in question
                    or any(synonym in question for synonym in metric.synonyms)
                },
                physical_binding_resolution=compilation.property_bindings,
            )
            try:
                state = graph.invoke(
                    {
                        "question": question,
                        "query_mode": "ontology",
                        "retry_count": 0,
                        "trace_steps": [],
                    }
                )
                report.generated_sql = state.get("generated_sql")
                join_plan = state.get("join_plan")
                report.join_resolution = (
                    [item.condition for item in join_plan.steps]
                    if hasattr(join_plan, "steps")
                    else []
                )
                report.sqlglot_valid = bool(
                    report.generated_sql
                    and sqlglot.parse_one(report.generated_sql, read="postgres")
                )
                validation = state.get("validation_report")
                report.ontology_policy_valid = bool(
                    validation and validation.valid
                )
                report.explain_passed = bool(
                    validation and validation.explain_passed
                )
                if state.get("status") != "success":
                    report.errors.append(
                        state.get("unsupported_reason") or "Semantic graph did not succeed"
                    )
                report.errors.extend(state.get("validation_errors", []))
            except Exception as exc:
                report.errors.append(str(exc))
            reports.append(report)
        return reports


class _FixedBundleRepository:
    """Strict-mode repository that never touches YAML or another ontology."""

    def __init__(self, bundle: OntologyBundle) -> None:
        self.bundle = bundle

    def load(self) -> OntologyBundle:
        return self.bundle
