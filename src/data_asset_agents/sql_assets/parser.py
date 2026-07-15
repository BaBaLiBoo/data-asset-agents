from __future__ import annotations

import json
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    ColumnReference,
    HistoricalSQLAnalysis,
    HistoricalSQLSummary,
    ParsedAggregate,
    ParsedJoin,
    UsageCount,
)


def _split_conjunction(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.And):
        return [
            *_split_conjunction(expression.left),
            *_split_conjunction(expression.right),
        ]
    return [expression]


class HistoricalSQLParser:
    """Parse certified historical SQL into deterministic structural evidence."""

    def parse(
        self,
        sql: str,
        *,
        question: str | None = None,
        certified: bool = False,
    ) -> HistoricalSQLAnalysis:
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise OntologyError(f"Historical SQL parse error: {exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise OntologyError("Historical SQL must contain exactly one read-only query")
        statement = statements[0]
        aliases: dict[str, str] = {}
        tables: list[str] = []
        for table in statement.find_all(exp.Table):
            aliases[table.alias_or_name] = table.name
            aliases[table.name] = table.name
            if table.name not in tables:
                tables.append(table.name)

        columns: list[ColumnReference] = []
        seen_columns: set[tuple[str | None, str]] = set()
        for column in statement.find_all(exp.Column):
            table_name = (
                aliases.get(column.table)
                if column.table
                else tables[0]
                if len(tables) == 1
                else None
            )
            key = (table_name, column.name)
            if key not in seen_columns:
                seen_columns.add(key)
                columns.append(ColumnReference(table=table_name, column=column.name))

        joins: list[ParsedJoin] = []
        for join in statement.find_all(exp.Join):
            on_expression = join.args.get("on")
            if on_expression is None:
                continue
            for equality in on_expression.find_all(exp.EQ):
                left = equality.left
                right = equality.right
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                left_table = aliases.get(left.table, left.table)
                right_table = aliases.get(right.table, right.table)
                if not left_table or not right_table:
                    continue
                joins.append(
                    ParsedJoin(
                        left_table=left_table,
                        left_column=left.name,
                        right_table=right_table,
                        right_column=right.name,
                        expression=(
                            f"{left_table}.{left.name} = "
                            f"{right_table}.{right.name}"
                        ),
                    )
                )

        where = statement.args.get("where")
        filters = (
            [item.sql(dialect="postgres") for item in _split_conjunction(where.this)]
            if where is not None
            else []
        )
        aggregates: list[ParsedAggregate] = []
        for aggregate in statement.find_all(exp.AggFunc):
            parent = aggregate.parent
            alias = parent.alias if isinstance(parent, exp.Alias) else None
            aggregates.append(
                ParsedAggregate(
                    function=aggregate.key.upper(),
                    expression=aggregate.sql(dialect="postgres"),
                    alias=alias or None,
                )
            )
        group = statement.args.get("group")
        group_by = (
            [item.sql(dialect="postgres") for item in group.expressions]
            if group is not None
            else []
        )
        time_fields = [
            column
            for column in columns
            if any(
                token in column.column.lower()
                for token in ("date", "time", "month", "year")
            )
        ]

        return HistoricalSQLAnalysis(
            question=question,
            sql_text=sql,
            certified=certified,
            tables=tables,
            columns=columns,
            joins=joins,
            filters=filters,
            aggregates=aggregates,
            group_by=group_by,
            time_fields=time_fields,
        )

    def parse_file(self, path: Path | str) -> list[HistoricalSQLAnalysis]:
        source_path = Path(path)
        try:
            payload: list[dict[str, Any]] = json.loads(
                source_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise OntologyError(
                f"Cannot load historical SQL file {source_path}: {exc}"
            ) from exc
        return [
            self.parse(
                str(item["sql"]),
                question=item.get("question"),
                certified=bool(item.get("certified", False)),
            )
            for item in payload
        ]

    def summarize(
        self, analyses: list[HistoricalSQLAnalysis]
    ) -> HistoricalSQLSummary:
        table_pairs: Counter[str] = Counter()
        column_usage: Counter[str] = Counter()
        join_usage: Counter[str] = Counter()
        for analysis in analyses:
            for left, right in combinations(sorted(set(analysis.tables)), 2):
                table_pairs[f"{left}|{right}"] += 1
            for column in analysis.columns:
                key = f"{column.table}.{column.column}" if column.table else column.column
                column_usage[key] += 1
            for join in analysis.joins:
                endpoints = sorted(
                    [
                        f"{join.left_table}.{join.left_column}",
                        f"{join.right_table}.{join.right_column}",
                    ]
                )
                join_usage[" = ".join(endpoints)] += 1
        return HistoricalSQLSummary(
            parsed_count=len(analyses),
            table_cooccurrence=self._usage_counts(table_pairs),
            column_usage=self._usage_counts(column_usage),
            join_usage=self._usage_counts(join_usage),
        )

    @staticmethod
    def _usage_counts(counter: Counter[str]) -> list[UsageCount]:
        return [
            UsageCount(key=key, count=count)
            for key, count in sorted(
                counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]
