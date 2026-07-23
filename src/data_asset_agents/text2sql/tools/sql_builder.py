import re
from datetime import date

from data_asset_agents.core.errors import UnsupportedQueryError
from data_asset_agents.ontology.models import Dimension, Metric
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.text2sql.models import JoinPlan, QueryFilter, SemanticQuery

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def generate_table_aliases(tables: list[str]) -> dict[str, str]:
    """Create stable collision-free aliases without domain-specific table names."""

    aliases: dict[str, str] = {}
    for index, table in enumerate(tables):
        if not IDENTIFIER.fullmatch(table):
            raise UnsupportedQueryError(f"Unsafe ontology table identifier: {table}")
        aliases[table] = f"t{index}"
    return aliases


def _qualified(table: str, column: str, aliases: dict[str, str]) -> str:
    if table not in aliases:
        raise UnsupportedQueryError(f"Table {table} is missing from the reviewed join plan")
    if not IDENTIFIER.fullmatch(column):
        raise UnsupportedQueryError(f"Unsafe ontology column identifier: {column}")
    return f"{aliases[table]}.{column}"


def _replace_qualifiers(expression: str, aliases: dict[str, str]) -> str:
    result = expression
    for table, alias in aliases.items():
        result = re.sub(rf"\b{re.escape(table)}\.", f"{alias}.", result)
    return result


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _reference_date_expression(ontology: OntologyService) -> str:
    """Return the governed snapshot date, or normal wall-clock SQL semantics."""

    raw_value = ontology.bundle.domain.get("data_reference_date")
    if raw_value is None:
        return "CURRENT_DATE"
    try:
        reference_date = date.fromisoformat(str(raw_value))
    except ValueError as exc:
        raise UnsupportedQueryError(
            "本体 data_reference_date 必须为 ISO 日期"
        ) from exc
    return f"DATE '{reference_date.isoformat()}'"


def _filter_predicate(
    query_filter: QueryFilter,
    base_table: str,
    aliases: dict[str, str],
) -> str:
    table = query_filter.table or base_table
    reference = _qualified(table, query_filter.field, aliases)
    operator = query_filter.operator.upper()
    if operator == "IN":
        values = [item.strip() for item in query_filter.value.split(",") if item.strip()]
        if not values:
            raise UnsupportedQueryError("IN 筛选条件不能为空")
        return f"{reference} IN ({', '.join(_literal(item) for item in values)})"
    if operator not in {"=", "!=", ">", ">=", "<", "<="}:
        raise UnsupportedQueryError(f"不支持的筛选操作符：{query_filter.operator}")
    return f"{reference} {operator} {_literal(query_filter.value)}"


def build_select_sql(
    ontology: OntologyService,
    semantic_query: SemanticQuery,
    metrics: list[Metric],
    dimensions: list[Dimension],
    join_plan: JoinPlan,
    resolved_filters: list[QueryFilter] | None = None,
) -> str:
    """Compile reviewed semantic definitions into deterministic PostgreSQL SQL."""

    if not metrics:
        raise UnsupportedQueryError("未识别到受支持的业务指标，无法生成 SQL")
    base_tables = {metric.base_table for metric in metrics}
    if len(base_tables) != 1:
        raise UnsupportedQueryError("MVP 暂不支持跨事实表指标组合")
    base_table = next(iter(base_tables))
    tables = list(join_plan.tables) or [base_table]
    if tables[0] != base_table:
        raise UnsupportedQueryError("Join 计划必须从指标事实表开始")
    aliases = generate_table_aliases(tables)

    selections: list[str] = []
    groups: list[str] = []
    output_aliases: dict[str, str] = {}
    for dimension in dimensions:
        reference = _qualified(dimension.table, dimension.column, aliases)
        selections.append(f"{reference} AS {dimension.column}")
        groups.append(reference)
        output_aliases[dimension.id] = dimension.column
    for metric in metrics:
        expression = _replace_qualifiers(metric.expression, aliases)
        selections.append(f"{expression} AS {metric.id}")
        output_aliases[metric.id] = metric.id

    sql_lines = [
        f"SELECT {', '.join(selections)}",
        f"FROM {base_table} {aliases[base_table]}",
    ]
    joined = {base_table}
    for step in join_plan.steps:
        if step.left_table not in joined or step.right_table in joined:
            raise UnsupportedQueryError("Join 计划中的表连接顺序无效")
        condition = (
            f"{_qualified(step.left_table, step.left_column, aliases)} = "
            f"{_qualified(step.right_table, step.right_column, aliases)}"
        )
        sql_lines.append(
            f"JOIN {step.right_table} {aliases[step.right_table]} ON {condition}"
        )
        joined.add(step.right_table)

    predicates: list[str] = []
    for metric in metrics:
        predicates.extend(
            f"{_qualified(metric.base_table, field, aliases)} = {_literal(value)}"
            for field, value in metric.required_filters.items()
        )
    predicates.extend(
        _filter_predicate(query_filter, base_table, aliases)
        for query_filter in (resolved_filters or [])
        if query_filter.source == "user"
    )

    if semantic_query.time_range.kind == "relative_days":
        days = semantic_query.time_range.days
        time_dimensions = {metric.time_dimension for metric in metrics}
        if not days or None in time_dimensions or len(time_dimensions) != 1:
            raise UnsupportedQueryError("指标缺少唯一、有效的审核时间维度")
        time_dimension_id = next(iter(time_dimensions))
        time_dimension = ontology.dimensions_by_id.get(str(time_dimension_id))
        if time_dimension is None:
            raise UnsupportedQueryError(
                f"未找到审核时间维度：{time_dimension_id}"
            )
        predicates.append(
            f"{_qualified(time_dimension.table, time_dimension.column, aliases)} "
            f">= {_reference_date_expression(ontology)} - INTERVAL '{days} days'"
        )
    elif semantic_query.time_range.kind == "absolute":
        time_dimensions = {metric.time_dimension for metric in metrics}
        if None in time_dimensions or len(time_dimensions) != 1:
            raise UnsupportedQueryError("指标缺少唯一、有效的审核时间维度")
        time_dimension = ontology.dimensions_by_id.get(str(next(iter(time_dimensions))))
        if time_dimension is None:
            raise UnsupportedQueryError("未找到审核时间维度")
        reference = _qualified(time_dimension.table, time_dimension.column, aliases)
        if semantic_query.time_range.start:
            predicates.append(
                f"{reference} >= {_literal(str(semantic_query.time_range.start))}"
            )
        if semantic_query.time_range.end:
            predicates.append(
                f"{reference} <= {_literal(str(semantic_query.time_range.end))}"
            )

    if predicates:
        sql_lines.append("WHERE " + " AND ".join(dict.fromkeys(predicates)))
    if groups:
        sql_lines.append("GROUP BY " + ", ".join(groups))
    if semantic_query.order_by:
        allowed_targets = {item.id for item in [*metrics, *dimensions]}
        ordering: list[str] = []
        for item in semantic_query.order_by:
            if item.target not in allowed_targets or not IDENTIFIER.fullmatch(item.target):
                raise UnsupportedQueryError(f"排序目标不是已选择的业务语义：{item.target}")
            ordering.append(
                f"{output_aliases[item.target]} {item.direction.upper()}"
            )
        sql_lines.append("ORDER BY " + ", ".join(ordering))
    elif groups:
        sql_lines.append("ORDER BY " + ", ".join(groups))
    limit = semantic_query.top_n or semantic_query.limit
    if limit:
        sql_lines.append(f"LIMIT {limit}")
    return "\n".join(sql_lines)
