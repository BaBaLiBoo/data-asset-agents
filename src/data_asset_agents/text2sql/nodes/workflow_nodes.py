from typing import Any

from data_asset_agents.core.errors import QueryExecutionError, UnsupportedQueryError
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.ontology.models import Dimension, Metric
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.compatibility import TemplateCompatibilityChecker
from data_asset_agents.sql_assets.models import SQLAssetSearchRequest, SQLRewriteResult
from data_asset_agents.sql_assets.repository import HistoricalSQLRepository
from data_asset_agents.sql_assets.rewriter import SQLTemplateRewriter
from data_asset_agents.sql_assets.service import SQLAssetService
from data_asset_agents.text2sql.models import (
    QueryFilter,
    TraceStep,
    ValidationIssue,
    ValidationReport,
)
from data_asset_agents.text2sql.state import Text2SQLState
from data_asset_agents.text2sql.tools import JoinPlanner, build_select_sql
from data_asset_agents.validation import (
    CommonSQLSafetyValidator,
    DatabaseCatalog,
    OntologyPolicyValidator,
    SQLRepairer,
)


def _trace(node: str, summary: str, status: str = "completed") -> list[TraceStep]:
    return [TraceStep(node=node, summary=summary, status=status)]  # type: ignore[arg-type]


class Text2SQLNodes:
    """Typed node implementations for the independently reusable Text-to-SQL subgraph."""

    def __init__(
        self,
        ontology: OntologyService,
        executor: ExecutorProtocol,
        history: HistoricalSQLRepository | None = None,
        sql_assets: SQLAssetService | None = None,
        history_enabled: bool = True,
    ) -> None:
        self.ontology = ontology
        self.executor = executor
        self.history = history or HistoricalSQLRepository()
        self.sql_assets = sql_assets
        self.history_enabled = history_enabled
        self.template_rewriter = (
            SQLTemplateRewriter(ontology, executor) if sql_assets is not None else None
        )
        self.template_checker = TemplateCompatibilityChecker(ontology)
        self.join_planner = JoinPlanner(ontology.bundle.joins)
        catalog = DatabaseCatalog.from_table_columns(
            {asset.name: set(asset.columns) for asset in ontology.bundle.tables}
        )
        self.common_validator = CommonSQLSafetyValidator(catalog)
        self.policy_validator = OntologyPolicyValidator(
            ontology.bundle, ontology.ontology_version_id
        )
        self.repairer = SQLRepairer()

    def parse_semantic_query(self, state: Text2SQLState) -> dict[str, Any]:
        semantic_query = self.ontology.parse(
            state["question"], state.get("matched_concepts")
        )
        supported = bool(semantic_query.metric_names)
        clarification = semantic_query.clarification_required
        return {
            "status": (
                "clarification_required"
                if clarification
                else ("success" if supported else "unsupported")
            ),
            "error_code": (
                "CLARIFICATION_REQUIRED"
                if clarification
                else (None if supported else "UNSUPPORTED_QUERY")
            ),
            "unsupported_reason": (
                semantic_query.clarification_question
                if clarification
                else (None if supported else "未识别到已发布本体支持的业务指标")
            ),
            "semantic_query": semantic_query,
            "metrics": semantic_query.metric_names,
            "dimensions": semantic_query.dimension_names,
            "filters": [item.model_dump() for item in semantic_query.filters],
            "time_range": semantic_query.time_range.model_dump(mode="json"),
            "retry_count": state.get("retry_count", 0),
            "trace_steps": _trace(
                "parse_semantic_query",
                f"识别 {len(semantic_query.metric_names)} 个指标、"
                f"{len(semantic_query.dimension_names)} 个维度",
            ),
        }

    def retrieve_business_concepts(self, state: Text2SQLState) -> dict[str, Any]:
        concepts = self.ontology.search(state["question"])
        return {
            "matched_concepts": concepts,
            "trace_steps": _trace(
                "retrieve_business_concepts", f"匹配 {len(concepts)} 个标准业务概念"
            ),
        }

    def resolve_physical_assets(self, state: Text2SQLState) -> dict[str, Any]:
        resolved = self.ontology.resolve(state["semantic_query"])
        metrics: list[Metric] = resolved.pop("metrics")  # type: ignore[assignment]
        dimensions: list[Dimension] = resolved.pop("dimensions")  # type: ignore[assignment]
        filters = resolved.pop("filters")
        return {
            **resolved,
            "metrics": [item.name for item in metrics],
            "dimensions": [item.name for item in dimensions],
            "filters": [item.model_dump() for item in filters],  # type: ignore[union-attr]
            "trace_steps": _trace(
                "resolve_physical_assets",
                f"确定 {len(resolved['selected_tables'])} 张正式表，"
                f"排除 {len(resolved['rejected_tables'])} 张干扰表",
            ),
        }

    def plan_join_path(self, state: Text2SQLState) -> dict[str, Any]:
        plan = self.join_planner.plan(state["selected_tables"])
        selected_columns = {
            table: list(columns)
            for table, columns in state["selected_columns"].items()
        }
        for step in plan.steps:
            selected_columns.setdefault(step.left_table, []).append(step.left_column)
            selected_columns.setdefault(step.right_table, []).append(step.right_column)
        selected_columns = {
            table: list(dict.fromkeys(columns)) for table, columns in selected_columns.items()
        }
        return {
            "join_plan": plan,
            "selected_tables": plan.tables,
            "selected_columns": selected_columns,
            "trace_steps": _trace("plan_join_path", f"规划 {len(plan.steps)} 条 Join 边"),
        }

    def retrieve_historical_sql(self, state: Text2SQLState) -> dict[str, Any]:
        if self.sql_assets is not None:
            results = self.sql_assets.search(
                SQLAssetSearchRequest(
                    question=state["question"],
                    semantic_query=state["semantic_query"],
                    selected_tables=state["selected_tables"],
                    selected_columns=state["selected_columns"],
                    join_conditions=[item.condition for item in state["join_plan"].steps],
                    limit=3,
                )
            )
            examples = [
                {
                    "question": item.asset.question,
                    "sql": item.asset.sql_text,
                    "certified": item.asset.certified,
                    "similarity": item.score.total,
                }
                for item in results
            ]
            selected = None
            selected_rank = None
            rejection_reasons: dict[str, list[str]] = {}
            for rank, result in enumerate(results, start=1):
                compatibility = self.template_checker.check(
                    result.asset,
                    state["question"],
                    state["semantic_query"],
                    state["selected_tables"],
                    state["selected_columns"],
                    state["join_plan"],
                )
                if compatibility.compatible:
                    selected = result.asset
                    selected_rank = rank
                    break
                rejection_reasons[result.asset.id] = compatibility.reasons
            return {
                "historical_sql_examples": examples,
                "sql_asset_candidates": results,
                "selected_sql_asset": selected,
                "selected_template_rank": selected_rank,
                "template_rejection_reasons": rejection_reasons,
                "trace_steps": _trace(
                    "retrieve_historical_sql",
                    f"召回 {len(results)} 条通过安全门槛的认证 SQL 资产",
                ),
            }
        examples = self.history.search(state["question"]) if self.history_enabled else []
        return {
            "historical_sql_examples": examples,
            "trace_steps": _trace(
                "retrieve_historical_sql", f"召回 {len(examples)} 条认证 SQL"
            ),
        }

    def generate_sql(self, state: Text2SQLState) -> dict[str, Any]:
        semantic = state["semantic_query"]
        metrics = self.ontology.get_metrics(semantic)
        dimensions = self.ontology.get_dimensions(semantic)
        try:
            deterministic_sql = build_select_sql(
                self.ontology,
                semantic,
                metrics,
                dimensions,
                state["join_plan"],
                [QueryFilter.model_validate(item) for item in state.get("filters", [])],
            )
        except UnsupportedQueryError as exc:
            return {
                "status": "unsupported",
                "error_code": exc.code,
                "unsupported_reason": str(exc),
                "generated_sql": None,
                "trace_steps": _trace("generate_sql", str(exc), "failed"),
            }
        selected_asset = state.get("selected_sql_asset")
        complex_template = bool(
            selected_asset
            and {"cte", "subquery", "window"} & set(selected_asset.structural_tags)
        )
        if (
            selected_asset is not None
            and self.template_rewriter is not None
            and complex_template
        ):
            concept_ids = {
                *(f"metric:{item.id}" for item in metrics),
                *(f"dimension:{item.id}" for item in dimensions),
            }
            mappings = [
                item
                for item in self.ontology.bundle.mappings
                if item.concept_id in concept_ids
            ]
            rewrite = self.template_rewriter.rewrite_or_fallback(
                selected_asset,
                deterministic_sql,
                semantic,
                mappings,
                state["join_plan"],
                [QueryFilter.model_validate(item) for item in state.get("filters", [])],
            )
            rewrite = rewrite.model_copy(
                update={
                    "selected_template_rank": state.get("selected_template_rank"),
                    "template_rejection_reasons": state.get(
                        "template_rejection_reasons", {}
                    ),
                }
            )
            return {
                "generated_sql": rewrite.rewritten_sql,
                "sql_rewrite": rewrite,
                "trace_steps": _trace(
                    "generate_sql",
                    "认证 SQL AST 模板改写通过"
                    if rewrite.used_template
                    else rewrite.fallback_reason or "回退确定性编译器",
                ),
            }
        if selected_asset is None and state.get("sql_asset_candidates"):
            reason = "Top-K 认证模板均不兼容，使用确定性编译器"
            return {
                "generated_sql": deterministic_sql,
                "sql_rewrite": SQLRewriteResult(
                    used_template=False,
                    selected_template_rank=None,
                    template_rejection_reasons=state.get(
                        "template_rejection_reasons", {}
                    ),
                    rewritten_sql=deterministic_sql,
                    fallback_reason=reason,
                ),
                "trace_steps": _trace("generate_sql", reason),
            }
        return {
            "generated_sql": deterministic_sql,
            "sql_rewrite": None,
            "trace_steps": _trace("generate_sql", "根据正式语义映射确定性编译 PostgreSQL SQL"),
        }

    def validate_sql(self, state: Text2SQLState) -> dict[str, Any]:
        sql = state.get("generated_sql")
        if not sql:
            issue = ValidationIssue(
                code="EMPTY_SQL",
                message="没有可供校验的 SQL",
            )
            report = ValidationReport(
                valid=False,
                errors=[issue.message],
                issues=[issue],
            )
            common_report = report
            policy_report = report
        else:
            required_filters = [QueryFilter.model_validate(item) for item in state["filters"]]
            common_report = self.common_validator.validate(
                sql, set(state["selected_tables"])
            )
            policy_report = self.policy_validator.validate(
                sql,
                semantic_query=state["semantic_query"],
                required_filters=required_filters,
                expected_version_id=self.ontology.ontology_version_id,
            )
            issues = list(common_report.issues)
            for policy_issue in policy_report.issues:
                if policy_issue not in issues:
                    issues.append(policy_issue)
            report = common_report.model_copy(
                update={
                    "valid": not issues,
                    "errors": [item.message for item in issues],
                    "issues": issues,
                }
            )
        return {
            "validation_report": report,
            "common_validation_report": common_report,
            "ontology_policy_report": policy_report,
            "validation_errors": report.errors,
            "trace_steps": _trace(
                "validate_sql",
                "SQLGlot 静态校验通过" if report.valid else "; ".join(report.errors),
                "completed" if report.valid else "failed",
            ),
        }

    def research_validation_error(self, state: Text2SQLState) -> dict[str, Any]:
        errors = state.get("validation_errors", [])
        repairable = self.repairer.can_repair(state["validation_report"].issues)
        return {
            "repairable": repairable,
            "trace_steps": _trace(
                "research_validation_error",
                f"定位校验错误：{'；'.join(errors)}；"
                f"确定性修复={'可用' if repairable else '不可用'}",
            )
        }

    def repair_sql(self, state: Text2SQLState) -> dict[str, Any]:
        original = state.get("generated_sql")
        if not original:
            return {
                "status": "failed",
                "error_code": "REPAIR_NOT_POSSIBLE",
                "repairable": False,
                "sql_changed": False,
                "trace_steps": _trace("repair_sql", "没有 SQL 可供修复", "failed"),
            }
        repaired, reason = self.repairer.repair(
            original,
            state["validation_report"].issues,
        )
        if repaired is None or repaired == original:
            return {
                "status": "failed",
                "error_code": "REPAIR_NOT_POSSIBLE",
                "repairable": False,
                "sql_changed": False,
                "trace_steps": _trace("repair_sql", reason, "failed"),
            }
        return {
            "generated_sql": repaired,
            "retry_count": state.get("retry_count", 0) + 1,
            "sql_changed": True,
            "trace_steps": _trace("repair_sql", reason),
        }

    def execute_sql(self, state: Text2SQLState) -> dict[str, Any]:
        try:
            sql = state.get("generated_sql")
            if not sql:
                raise QueryExecutionError("没有通过校验的 SQL 可供执行")
            result = self.executor.execute(sql, set(state["selected_tables"]))
        except QueryExecutionError as exc:
            errors = [*state.get("validation_errors", []), str(exc)]
            report = state["validation_report"].model_copy(
                update={
                    "valid": False,
                    "explain_passed": False,
                    "errors": errors,
                    "issues": [
                        *state["validation_report"].issues,
                        ValidationIssue(code="EXECUTION_ERROR", message=str(exc)),
                    ],
                }
            )
            return {
                "validation_report": report,
                "validation_errors": errors,
                "trace_steps": _trace("execute_sql", str(exc), "failed"),
            }
        report = state["validation_report"].model_copy(update={"explain_passed": True})
        return {
            "status": "success",
            "execution_result": result,
            "validation_report": report,
            "trace_steps": _trace(
                "execute_sql", f"PostgreSQL EXPLAIN 通过并返回 {result.row_count} 行"
            ),
        }

    def explain_result(self, state: Text2SQLState) -> dict[str, Any]:
        if state.get("status") == "clarification_required":
            explanation = (
                state.get("unsupported_reason")
                or "问题存在多义性，请补充标准指标或维度。"
            )
            confidence = state.get("semantic_query").confidence
        elif state.get("status") == "unsupported":
            explanation = state.get("unsupported_reason") or "该问题不在第一阶段支持范围内。"
            confidence = 0.0
        elif state.get("execution_result") is not None:
            rejected = "、".join(item.name for item in state.get("rejected_tables", []))
            explanation = (
                f"已按本体口径计算{'、'.join(state.get('metrics', []))}，"
                f"按{'、'.join(state.get('dimensions', []))}分组。"
                f"使用表 {'、'.join(state.get('selected_tables', []))}；"
                f"生命周期策略排除了 {rejected or '无'}。"
            )
            confidence = 0.96
        else:
            explanation = "SQL 在最大修复次数内未通过安全校验，未执行数据库查询。"
            confidence = 0.2
        return {
            "status": state.get("status", "failed") if confidence != 0.2 else "failed",
            "explanation": explanation,
            "confidence": confidence,
            "trace_steps": _trace("explain_result", explanation),
        }
