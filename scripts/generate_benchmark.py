"""Generate the reviewed MiniBank benchmark from deterministic Gold SQL templates."""

from __future__ import annotations

import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp

OUTPUT = Path("data/benchmark/text2sql_v1.json")
HASHES_PATH = Path("data/benchmark/text2sql_v1_hashes.json")
PLACEHOLDER_HASH = "0" * 64
REFERENCE_DATE = "2026-07-16"


def load_reviewed_hashes() -> dict[str, str]:
    """Load hashes materialized against the fixed MiniBank seed, when available."""

    if not HASHES_PATH.exists():
        return {}
    payload = json.loads(HASHES_PATH.read_text(encoding="utf-8"))
    return {str(case_id): str(value) for case_id, value in payload.items()}


REVIEWED_HASHES = load_reviewed_hashes()


def references(sql: str) -> tuple[list[str], list[str]]:
    statement = sqlglot.parse_one(sql, read="postgres")
    ctes = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    aliases = {table.alias_or_name: table.name for table in statement.find_all(exp.Table)}
    tables = sorted(
        {table.name for table in statement.find_all(exp.Table) if table.name not in ctes}
    )
    columns = sorted(
        {
            f"{aliases[column.table]}.{column.name}"
            for column in statement.find_all(exp.Column)
            if column.table in aliases and aliases[column.table] in tables
        }
    )
    return tables, columns


def gold_time_range(sql: str) -> dict[str, object]:
    """Derive the reviewed semantic time contract from controlled DSL SQL."""

    relative = re.search(
        r"(?:CURRENT_DATE|DATE\s*'\d{4}-\d{2}-\d{2}')\s*-\s*"
        r"INTERVAL\s*'(\d+) days'",
        sql,
    )
    if relative:
        return {"kind": "relative_days", "days": int(relative.group(1))}
    dates = re.findall(r"DATE\s*'(\d{4}-\d{2}-\d{2})'", sql)
    if dates:
        return {
            "kind": "absolute",
            "start": dates[0],
            "end": dates[-1] if len(dates) > 1 else dates[0],
        }
    return {"kind": "none"}


def success_case(
    case_id: str,
    question: str,
    category: str,
    difficulty: str,
    sql: str,
    metrics: list[str],
    dimensions: list[str],
    *,
    filters: list[dict[str, object]] | None = None,
    joins: list[str] | None = None,
    tags: list[str] | None = None,
    ordered: bool = False,
) -> dict[str, object]:
    tables, columns = references(sql)
    return {
        "id": case_id,
        "question": question,
        "category": category,
        "difficulty": difficulty,
        "expected_status": "success",
        "gold_metric_ids": metrics,
        "gold_dimension_ids": dimensions,
        "gold_filters": filters or [],
        "gold_semantic_filters": [],
        "gold_time_range": gold_time_range(sql),
        "gold_tables": tables,
        "gold_columns": columns,
        "gold_joins": joins or [],
        "gold_sql": sql,
        "expected_result_hash": REVIEWED_HASHES.get(case_id, PLACEHOLDER_HASH),
        "result_order_sensitive": ordered,
        "tags": tags or [],
        "notes": "Gold SQL is authored by the controlled benchmark DSL templates.",
    }


POSTED = {
    "table": "dwd_card_transaction",
    "field": "transaction_status",
    "operator": "=",
    "value": "POSTED",
    "source": "metric_policy",
}
CREDIT = {
    "table": "dwd_card_transaction",
    "field": "card_type",
    "operator": "=",
    "value": "CREDIT",
    "source": "metric_policy",
}
BRANCH_JOIN = "dwd_card_transaction.branch_id = dim_branch.branch_id"
CUSTOMER_JOIN = "dwd_card_transaction.customer_id = dim_customer.customer_id"
MERCHANT_JOIN = "dwd_card_transaction.merchant_id = dim_merchant.merchant_id"


def build_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    base_templates = [
        (
            "transaction_amount",
            "交易金额",
            "SUM(t.txn_amount_cny) AS transaction_amount",
            [POSTED],
            "t.transaction_status = 'POSTED'",
        ),
        (
            "credit_card_transaction_amount",
            "信用卡交易金额",
            "SUM(t.txn_amount_cny) AS credit_card_transaction_amount",
            [POSTED, CREDIT],
            "t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT'",
        ),
        (
            "transaction_count",
            "交易笔数",
            "COUNT(DISTINCT t.transaction_id) AS transaction_count",
            [POSTED],
            "t.transaction_status = 'POSTED'",
        ),
        (
            "credit_card_transaction_count",
            "信用卡交易笔数",
            "COUNT(DISTINCT t.transaction_id) AS credit_card_transaction_count",
            [POSTED, CREDIT],
            "t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT'",
        ),
        (
            "average_transaction_amount",
            "平均交易金额",
            "AVG(t.txn_amount_cny) AS average_transaction_amount",
            [POSTED],
            "t.transaction_status = 'POSTED'",
        ),
        (
            "active_customer_count",
            "活跃客户数",
            "COUNT(DISTINCT t.customer_id) AS active_customer_count",
            [POSTED],
            "t.transaction_status = 'POSTED'",
        ),
    ]
    basic_questions = [
        "查询交易金额",
        "统计信用卡交易金额",
        "查询交易笔数",
        "统计信用卡交易笔数",
        "查询平均交易金额",
        "查询活跃客户数",
        "统计全部已入账交易额",
        "计算贷记卡消费金额",
        "统计流水笔数",
        "查询客单价",
    ]
    basic_template_indexes = [0, 1, 2, 3, 4, 5, 0, 1, 2, 4]
    for index, question in enumerate(basic_questions):
        metric_id, _, expression, filters, where = base_templates[
            basic_template_indexes[index]
        ]
        sql = f"SELECT {expression} FROM dwd_card_transaction t WHERE {where}"
        cases.append(
            success_case(
                f"basic-{index + 1:02d}",
                question,
                "基础指标",
                "easy",
                sql,
                [metric_id],
                [],
                filters=filters,
                tags=["business_policy"],
            )
        )

    dimensions = [
        (
            "branch",
            "分行",
            "b.branch_name",
            "JOIN dim_branch b ON t.branch_id = b.branch_id",
            [BRANCH_JOIN],
        ),
        ("transaction_channel", "交易渠道", "t.transaction_channel", "", []),
        ("card_type", "卡类型", "t.card_type", "", []),
        ("transaction_date", "交易日期", "t.transaction_date", "", []),
    ]
    for index in range(10):
        dimension_id, name, group_column, join_sql, joins = dimensions[index % 4]
        second = index >= 6
        select_dimension = group_column
        group_by = group_column
        dimension_ids = [dimension_id]
        if second and dimension_id != "transaction_channel":
            select_dimension += ", t.transaction_channel"
            group_by += ", t.transaction_channel"
            dimension_ids.append("transaction_channel")
        sql = (
            f"SELECT {select_dimension}, SUM(t.txn_amount_cny) AS transaction_amount "
            f"FROM dwd_card_transaction t {join_sql} "
            "WHERE t.transaction_status = 'POSTED' "
            f"GROUP BY {group_by} ORDER BY {group_by}"
        )
        cases.append(
            success_case(
                f"dimension-{index + 1:02d}",
                f"按{name}{'和交易渠道' if len(dimension_ids) == 2 else ''}统计交易金额",
                "单维度、双维度",
                "medium" if second else "easy",
                sql,
                ["transaction_amount"],
                dimension_ids,
                filters=[POSTED],
                joins=joins,
                tags=["business_policy"],
                ordered=True,
            )
        )

    join_dimensions = [
        (
            "branch",
            "分行",
            "b.branch_name",
            "JOIN dim_branch b ON t.branch_id = b.branch_id",
            BRANCH_JOIN,
        ),
        (
            "customer_type",
            "客户类型",
            "c.customer_type",
            "JOIN dim_customer c ON t.customer_id = c.customer_id",
            CUSTOMER_JOIN,
        ),
        (
            "merchant_category",
            "商户类别",
            "m.merchant_category",
            "JOIN dim_merchant m ON t.merchant_id = m.merchant_id",
            MERCHANT_JOIN,
        ),
    ]
    for index in range(10):
        first = join_dimensions[index % 3]
        second = join_dimensions[(index + 1) % 3]
        use_two = index >= 4
        selects = first[2] + (f", {second[2]}" if use_two else "")
        joins_sql = first[3] + (f" {second[3]}" if use_two else "")
        groups = selects
        join_edges = [first[4]] + ([second[4]] if use_two else [])
        dimension_ids = [first[0]] + ([second[0]] if use_two else [])
        sql = (
            f"SELECT {selects}, COUNT(DISTINCT t.transaction_id) AS transaction_count "
            f"FROM dwd_card_transaction t {joins_sql} "
            "WHERE t.transaction_status = 'POSTED' "
            f"GROUP BY {groups} ORDER BY {groups}"
        )
        cases.append(
            success_case(
                f"join-{index + 1:02d}",
                f"按{first[1]}{'和' + second[1] if use_two else ''}统计交易笔数",
                "多表 Join",
                "hard" if use_two else "medium",
                sql,
                ["transaction_count"],
                dimension_ids,
                filters=[POSTED],
                joins=join_edges,
                tags=["business_policy", "multi_table_join"],
                ordered=True,
            )
        )

    time_filters = [
        ("近7天", f"t.transaction_date >= DATE '{REFERENCE_DATE}' - INTERVAL '7 days'"),
        ("近30天", f"t.transaction_date >= DATE '{REFERENCE_DATE}' - INTERVAL '30 days'"),
        ("近60天", f"t.transaction_date >= DATE '{REFERENCE_DATE}' - INTERVAL '60 days'"),
        ("最近两周", f"t.transaction_date >= DATE '{REFERENCE_DATE}' - INTERVAL '14 days'"),
        ("2026年7月", "t.transaction_date BETWEEN DATE '2026-07-01' AND DATE '2026-07-31'"),
    ]
    for index in range(10):
        label, time_sql = time_filters[index % 5]
        channel = index >= 5
        extra = " AND t.transaction_channel IN ('APP', 'POS')" if channel else ""
        sql = (
            "SELECT SUM(t.txn_amount_cny) AS credit_card_transaction_amount "
            "FROM dwd_card_transaction t WHERE t.transaction_status = 'POSTED' "
            f"AND t.card_type = 'CREDIT' AND {time_sql}{extra}"
        )
        cases.append(
            success_case(
                f"filter-{index + 1:02d}",
                f"查询{label}{'APP和POS渠道' if channel else ''}信用卡交易金额",
                "时间、用户过滤和 IN",
                "medium",
                sql,
                ["credit_card_transaction_amount"],
                [],
                filters=[POSTED, CREDIT],
                tags=["business_policy", "time_filter"] + (["in_filter"] if channel else []),
            )
        )

    for index in range(8):
        limit = index + 3
        sql = (
            "SELECT b.branch_name, SUM(t.txn_amount_cny) AS credit_card_transaction_amount "
            "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
            "WHERE t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT' "
            "GROUP BY b.branch_name ORDER BY credit_card_transaction_amount DESC "
            f"LIMIT {limit}"
        )
        cases.append(
            success_case(
                f"top-{index + 1:02d}",
                f"查询信用卡交易金额最高的前{limit}个分行",
                "Top N 和排序",
                "medium",
                sql,
                ["credit_card_transaction_amount"],
                ["branch"],
                filters=[POSTED, CREDIT],
                joins=[BRANCH_JOIN],
                tags=["business_policy", "top_n"],
                ordered=True,
            )
        )

    for index in range(10):
        structure = index % 3
        if structure == 0:
            sql = (
                "WITH totals AS (SELECT b.branch_name, SUM(t.txn_amount_cny) AS amount "
                "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
                "WHERE t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT' "
                "GROUP BY b.branch_name) SELECT branch_name, amount, "
                "DENSE_RANK() OVER (ORDER BY amount DESC) AS amount_rank "
                "FROM totals ORDER BY amount_rank"
            )
        elif structure == 1:
            sql = (
                "SELECT b.branch_name, SUM(t.txn_amount_cny) AS amount FROM dwd_card_transaction t "
                "JOIN dim_branch b ON t.branch_id = b.branch_id "
                "WHERE t.transaction_status = 'POSTED' AND t.txn_amount_cny > "
                "(SELECT AVG(x.txn_amount_cny) FROM dwd_card_transaction x "
                "WHERE x.transaction_status = 'POSTED') GROUP BY b.branch_name ORDER BY amount DESC"
            )
        else:
            sql = (
                "WITH daily AS (SELECT t.transaction_date, SUM(t.txn_amount_cny) AS amount "
                "FROM dwd_card_transaction t WHERE t.transaction_status = 'POSTED' "
                "GROUP BY t.transaction_date) SELECT transaction_date, amount, "
                "SUM(amount) OVER (ORDER BY transaction_date) AS running_amount "
                "FROM daily ORDER BY transaction_date"
            )
        cases.append(
            success_case(
                f"complex-{index + 1:02d}",
                [
                    "查询各分行信用卡交易金额和排名",
                    "查询高于整体平均值的分行交易金额",
                    "查询每日交易金额和累计金额",
                ][structure],
                "CTE、窗口函数和子查询",
                "hard",
                sql,
                ["credit_card_transaction_amount" if structure == 0 else "transaction_amount"],
                ["branch" if structure in {0, 1} else "transaction_date"],
                filters=[POSTED] + ([CREDIT] if structure == 0 else []),
                joins=[BRANCH_JOIN] if structure in {0, 1} else [],
                tags=["business_policy", ["cte", "window", "subquery"][structure]],
                ordered=True,
            )
        )

    branch_credit_amount_sql = (
        "SELECT b.branch_name, SUM(t.txn_amount_cny) AS credit_card_transaction_amount "
        "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
        "WHERE t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT' "
        "GROUP BY b.branch_name ORDER BY b.branch_name"
    )
    synonym_cases = [
        (
            "按机构查询消费金额",
            branch_credit_amount_sql,
            ["credit_card_transaction_amount"],
            ["branch"],
            [POSTED, CREDIT],
            [BRANCH_JOIN],
        ),
        (
            "各网点贷记卡交易额",
            branch_credit_amount_sql,
            ["credit_card_transaction_amount"],
            ["branch"],
            [POSTED, CREDIT],
            [BRANCH_JOIN],
        ),
        (
            "按渠道统计流水笔数",
            "SELECT t.transaction_channel, COUNT(DISTINCT t.transaction_id) AS transaction_count "
            "FROM dwd_card_transaction t WHERE t.transaction_status = 'POSTED' "
            "GROUP BY t.transaction_channel ORDER BY t.transaction_channel",
            ["transaction_count"],
            ["transaction_channel"],
            [POSTED],
            [],
        ),
        (
            "查询客单价",
            "SELECT AVG(t.txn_amount_cny) AS average_transaction_amount "
            "FROM dwd_card_transaction t WHERE t.transaction_status = 'POSTED'",
            ["average_transaction_amount"],
            [],
            [POSTED],
            [],
        ),
        (
            "统计各机构信用卡消费金额",
            branch_credit_amount_sql,
            ["credit_card_transaction_amount"],
            ["branch"],
            [POSTED, CREDIT],
            [BRANCH_JOIN],
        ),
        (
            "近30天网点交易额",
            "SELECT b.branch_name, SUM(t.txn_amount_cny) AS transaction_amount "
            "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
            f"WHERE t.transaction_status = 'POSTED' AND t.transaction_date >= "
            f"DATE '{REFERENCE_DATE}' - INTERVAL '30 days' "
            "GROUP BY b.branch_name ORDER BY b.branch_name",
            ["transaction_amount"],
            ["branch"],
            [POSTED],
            [BRANCH_JOIN],
        ),
        (
            "按交易日查询贷记卡流水笔数",
            "SELECT t.transaction_date, COUNT(DISTINCT t.transaction_id) "
            "AS credit_card_transaction_count FROM dwd_card_transaction t "
            "WHERE t.transaction_status = 'POSTED' AND t.card_type = 'CREDIT' "
            "GROUP BY t.transaction_date ORDER BY t.transaction_date",
            ["credit_card_transaction_count"],
            ["transaction_date"],
            [POSTED, CREDIT],
            [],
        ),
        (
            "各分行人民币交易金额",
            "SELECT b.branch_name, SUM(t.txn_amount_cny) AS transaction_amount "
            "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
            "WHERE t.transaction_status = 'POSTED' GROUP BY b.branch_name ORDER BY b.branch_name",
            ["transaction_amount"],
            ["branch"],
            [POSTED],
            [BRANCH_JOIN],
        ),
    ]
    for index, (question, sql, metrics, dimensions, filters, joins) in enumerate(
        synonym_cases
    ):
        cases.append(
            success_case(
                f"synonym-{index + 1:02d}",
                question,
                "同义词与金额字段歧义",
                "hard",
                sql,
                metrics,
                dimensions,
                filters=filters,
                joins=joins,
                tags=["business_policy", "synonym", "amount_field_ambiguity"],
                ordered=True,
            )
        )

    for index in range(8):
        sql = (
            "SELECT b.branch_name, COUNT(DISTINCT t.transaction_id) AS transaction_count "
            "FROM dwd_card_transaction t JOIN dim_branch b ON t.branch_id = b.branch_id "
            "WHERE t.transaction_status = 'POSTED' GROUP BY b.branch_name ORDER BY b.branch_name"
        )
        cases.append(
            success_case(
                f"lifecycle-{index + 1:02d}",
                f"在当前正式明细资产中按分行统计交易笔数（场景{index + 1}）",
                "生命周期、汇总粒度干扰",
                "hard",
                sql,
                ["transaction_count"],
                ["branch"],
                filters=[POSTED],
                joins=[BRANCH_JOIN],
                tags=["business_policy", "lifecycle_distractor", "grain_distractor"],
                ordered=True,
            )
        )

    nonsuccess = [
        ("clarification-01", "查询交易情况", "clarification_required"),
        ("clarification-02", "看看最近的金额", "clarification_required"),
        ("clarification-03", "统计机构数据", "clarification_required"),
        ("unsupported-01", "查询明天的天气", "unsupported"),
        ("unsupported-02", "查询股票价格", "unsupported"),
        ("unsupported-03", "帮我写一首诗", "unsupported"),
    ]
    for case_id, question, status in nonsuccess:
        cases.append(
            {
                "id": case_id,
                "question": question,
                "category": "澄清与域外",
                "difficulty": "medium",
                "expected_status": status,
                "gold_metric_ids": [],
                "gold_dimension_ids": [],
                "gold_filters": [],
                "gold_tables": [],
                "gold_columns": [],
                "gold_joins": [],
                "gold_sql": None,
                "expected_result_hash": None,
                "result_order_sensitive": False,
                "tags": [status],
                "notes": "No SQL should be generated.",
            }
        )
    assert len(cases) == 80
    return cases


def main() -> None:
    payload = {
        "version": "minibank-text2sql-v1",
        "description": "80-case fictional MiniBank controlled Text-to-SQL benchmark",
        "cases": build_cases(),
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
