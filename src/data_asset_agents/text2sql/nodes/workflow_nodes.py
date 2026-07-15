from typing import Any

from data_asset_agents.core.errors import QueryExecutionError
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.ontology.models import Dimension, Metric
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.repository import HistoricalSQLRepository
from data_asset_agents.text2sql.models import TraceStep
from data_asset_agents.text2sql.state import Text2SQLState
from data_asset_agents.text2sql.tools import JoinPlanner
from data_asset_agents.validation import SQLValidator


def _trace(node: str, summary: str, status: str = "completed") -> list[TraceStep]:
    return [TraceStep(node=node, summary=summary, status=status)]  # type: ignore[arg-type]


class Text2SQLNodes:
    """Typed node implementations for the independently reusable Text-to-SQL subgraph."""

    def __init__(
        self,
        ontology: OntologyService,
        executor: ExecutorProtocol,
        history: HistoricalSQLRepository | None = None,
    ) -> None:
        self.ontology = ontology
        self.executor = executor
        self.history = history or HistoricalSQLRepository()
        self.join_planner = JoinPlanner(ontology.bundle.joins)
        self.validator = SQLValidator()

    def parse_semantic_query(self, state: Text2SQLState) -> dict[str, Any]:
        semantic_query = self.ontology.parse(state["question"])
        return {
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
        selected_columns = {table: list(columns) for table, columns in state["selected_columns"].items()}
        for step in plan.steps:
            left_reference, right_reference = step.condition.split(" = ")
            left_table, left_column = left_reference.split(".", maxsplit=1)
            right_table, right_column = right_reference.split(".", maxsplit=1)
            selected_columns.setdefault(left_table, []).append(left_column)
            selected_columns.setdefault(right_table, []).append(right_column)
        selected_columns = {
            table: list(dict.fromkeys(columns)) for table, columns in selected_columns.items()
        }
        return {
            "join_plan": plan,
            "selected_columns": selected_columns,
            "trace_steps": _trace("plan_join_path", f"规划 {len(plan.steps)} 条 Join 边"),
        }

    def retrieve_historical_sql(self, state: Text2SQLState) -> dict[str, Any]:
        examples = self.history.search(state["question"])
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
        if not metrics:
            sql = "SELECT 1 AS unsupported_query"
        else:
            aliases = {"dwd_card_transaction": "t", "dim_branch": "b"}
            selections: list[str] = []
            groups: list[str] = []
            for dimension in dimensions:
                reference = f"{aliases.get(dimension.table, dimension.table)}.{dimension.column}"
                selections.append(f"{reference} AS {dimension.id}")
                groups.append(reference)
            for metric in metrics:
                expression = metric.expression
                for table, alias in aliases.items():
                    expression = expression.replace(f"{table}.", f"{alias}.")
                selections.append(f"{expression} AS {metric.id}")
            base_table = metrics[0].base_table
            sql_lines = [f"SELECT {', '.join(selections)}", f"FROM {base_table} {aliases.get(base_table, '')}"]
            for step in state["join_plan"].steps:
                right = step.right_table
                condition = step.condition
                for table, alias in aliases.items():
                    condition = condition.replace(f"{table}.", f"{alias}.")
                sql_lines.append(f"JOIN {right} {aliases.get(right, '')} ON {condition}")
            predicates: list[str] = []
            for metric in metrics:
                for field, value in metric.required_filters.items():
                    predicates.append(f"{aliases.get(metric.base_table, metric.base_table)}.{field} = '{value}'")
            if semantic.time_range.kind == "relative_days" and semantic.time_range.days:
                predicates.append(
                    f"{aliases.get(base_table, base_table)}.transaction_date "
                    f">= CURRENT_DATE - INTERVAL '{semantic.time_range.days} days'"
                )
            if predicates:
                sql_lines.append("WHERE " + " AND ".join(dict.fromkeys(predicates)))
            if groups:
                sql_lines.append("GROUP BY " + ", ".join(groups))
                sql_lines.append("ORDER BY " + ", ".join(groups))
            sql = "\n".join(sql_lines)
        return {
            "generated_sql": sql,
            "trace_steps": _trace("generate_sql", "根据正式语义映射生成 PostgreSQL SQL"),
        }

    def validate_sql(self, state: Text2SQLState) -> dict[str, Any]:
        report = self.validator.validate(state["generated_sql"], set(state["selected_tables"]))
        return {
            "validation_report": report,
            "validation_errors": report.errors,
            "trace_steps": _trace(
                "validate_sql",
                "SQLGlot 静态校验通过" if report.valid else "; ".join(report.errors),
                "completed" if report.valid else "failed",
            ),
        }

    def research_validation_error(self, state: Text2SQLState) -> dict[str, Any]:
        errors = state.get("validation_errors", [])
        return {
            "trace_steps": _trace(
                "research_validation_error", f"定位校验错误：{'；'.join(errors)}"
            )
        }

    def repair_sql(self, state: Text2SQLState) -> dict[str, Any]:
        report = state.get("validation_report")
        repaired = report.formatted_sql if report and report.formatted_sql else state["generated_sql"]
        return {
            "generated_sql": repaired,
            "retry_count": state.get("retry_count", 0) + 1,
            "trace_steps": _trace("repair_sql", "使用解析结果和允许表集合修复 SQL"),
        }

    def execute_sql(self, state: Text2SQLState) -> dict[str, Any]:
        try:
            result = self.executor.execute(state["generated_sql"], set(state["selected_tables"]))
        except QueryExecutionError as exc:
            errors = [*state.get("validation_errors", []), str(exc)]
            report = state["validation_report"].model_copy(
                update={"valid": False, "explain_passed": False, "errors": errors}
            )
            return {
                "validation_report": report,
                "validation_errors": errors,
                "trace_steps": _trace("execute_sql", str(exc), "failed"),
            }
        report = state["validation_report"].model_copy(update={"explain_passed": True})
        return {
            "execution_result": result,
            "validation_report": report,
            "trace_steps": _trace(
                "execute_sql", f"PostgreSQL EXPLAIN 通过并返回 {result.row_count} 行"
            ),
        }

    def explain_result(self, state: Text2SQLState) -> dict[str, Any]:
        if state.get("execution_result") is not None:
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
            "explanation": explanation,
            "confidence": confidence,
            "trace_steps": _trace("explain_result", explanation),
        }
