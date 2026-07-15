import sqlglot
from sqlglot import exp

from data_asset_agents.text2sql.models import ValidationIssue


class SQLRepairer:
    """Apply a small allowlisted set of deterministic repairs to validated SQL."""

    REPAIRABLE_CODES = {"MISSING_REQUIRED_FILTER"}

    def can_repair(self, issues: list[ValidationIssue]) -> bool:
        return bool(issues) and all(issue.code in self.REPAIRABLE_CODES for issue in issues)

    def repair(self, sql: str, issues: list[ValidationIssue]) -> tuple[str | None, str]:
        if not self.can_repair(issues):
            return None, "校验错误不属于确定性修复白名单"
        statement = sqlglot.parse_one(sql, read="postgres")
        aliases = {
            table.name: table.alias_or_name for table in statement.find_all(exp.Table)
        }
        predicates: list[exp.Expression] = []
        for issue in issues:
            if not issue.table or not issue.column or issue.expected_value is None:
                return None, "必要过滤条件缺少结构化上下文"
            alias = aliases.get(issue.table)
            if alias is None:
                return None, f"SQL 中不存在待修复表 {issue.table}"
            predicates.append(
                exp.EQ(
                    this=exp.column(issue.column, table=alias),
                    expression=exp.Literal.string(issue.expected_value),
                )
            )
        predicate = exp.and_(*predicates)
        existing_where = statement.args.get("where")
        if existing_where is None:
            statement.set("where", exp.Where(this=predicate))
        else:
            statement.set(
                "where",
                exp.Where(this=exp.and_(existing_where.this, predicate)),
            )
        repaired = statement.sql(dialect="postgres", pretty=True)
        original = sqlglot.parse_one(sql, read="postgres").sql(
            dialect="postgres", pretty=True
        )
        if repaired == original:
            return None, "修复前后 SQL 未发生变化"
        return repaired, "补充缺失的审核指标过滤条件"
