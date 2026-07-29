from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from .models import AssetInput, LayerScore
from .similarity_utils import jaccard, multiset_jaccard, weighted_available


LOGIC_WEIGHTS = {
    "input": 0.15,
    "projection": 0.20,
    "join": 0.15,
    "predicate": 0.20,
    "group_agg": 0.20,
    "structure": 0.10,
}

PREDICATE_WEIGHTS = {
    "field": 0.40,
    "operator": 0.25,
    "value": 0.25,
    "boolean": 0.10,
}


@dataclass(frozen=True, slots=True)
class PredicateAtom:
    fields: tuple[str, ...]
    operator: str
    exact_values: tuple[str, ...]
    value_categories: tuple[str, ...]
    parameterized: bool

    @property
    def field_key(self) -> str:
        return "|".join(self.fields)

    @property
    def operator_key(self) -> str:
        return f"{self.field_key}:{self.operator}"


@dataclass(frozen=True, slots=True)
class SQLFingerprint:
    parse_success: bool
    input_tables: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    projections: tuple[str, ...] = ()
    joins: tuple[str, ...] = ()
    predicates: tuple[PredicateAtom, ...] = ()
    boolean_structure: tuple[str, ...] = ()
    group_expressions: tuple[str, ...] = ()
    aggregate_expressions: tuple[str, ...] = ()
    structures: tuple[str, ...] = ()
    warning: str | None = None


def _is_star(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.Star) or (
        isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)
    )


def _is_identity_where(where: exp.Expression | None) -> bool:
    if where is None:
        return True
    node = where.this if isinstance(where, exp.Where) else where
    if not isinstance(node, exp.EQ):
        return False
    left = node.left
    right = node.right
    return (
        isinstance(left, exp.Literal)
        and isinstance(right, exp.Literal)
        and left.this == right.this
    )


def _simple_star_select(select: exp.Select) -> bool:
    if len(select.expressions) != 1 or not _is_star(select.expressions[0]):
        return False
    forbidden = ("joins", "group", "having", "qualify", "order", "limit", "distinct")
    if any(select.args.get(name) for name in forbidden):
        return False
    return _is_identity_where(select.args.get("where"))


def unwrap_identity_layers(root: exp.Expression) -> exp.Expression:
    current = root
    for _ in range(8):
        if not isinstance(current, exp.Select) or not _simple_star_select(current):
            break
        from_clause = current.args.get("from_")
        source = from_clause.this if from_clause is not None else None
        if isinstance(source, exp.Subquery):
            current = source.this.copy()
            continue
        with_clause = current.args.get("with_")
        if isinstance(source, exp.Table) and with_clause and len(with_clause.expressions) == 1:
            cte = with_clause.expressions[0]
            if isinstance(cte, exp.CTE) and source.name.lower() == cte.alias_or_name.lower():
                current = cte.this.copy()
                continue
        break
    return current


def _flatten_boolean(node: exp.Expression, kind: type[exp.Expression]) -> list[exp.Expression]:
    if isinstance(node, kind):
        return [
            *_flatten_boolean(node.left, kind),
            *_flatten_boolean(node.right, kind),
        ]
    return [node]


def canonical_expression(node: exp.Expression) -> str:
    if isinstance(node, exp.Alias):
        return canonical_expression(node.this)
    if isinstance(node, exp.Paren):
        return canonical_expression(node.this)
    if isinstance(node, (exp.And, exp.Or)):
        items = sorted(canonical_expression(item) for item in _flatten_boolean(node, type(node)))
        separator = " and " if isinstance(node, exp.And) else " or "
        return f"({separator.join(items)})"
    copied = node.copy()

    def transform(item: exp.Expression) -> exp.Expression:
        if isinstance(item, exp.Column):
            if isinstance(item.this, exp.Star):
                return exp.Star()
            return exp.column(item.name.lower())
        if isinstance(item, exp.Identifier):
            return exp.Identifier(this=item.this.lower(), quoted=False)
        return item

    copied = copied.transform(transform)
    return re.sub(r"\s+", " ", copied.sql(dialect="spark")).strip().lower()


def _literal_category(literal: exp.Literal) -> str:
    value = str(literal.this)
    if not literal.is_string:
        return "number"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ t].*)?", value):
        return "date"
    return "string"


def _predicate_atoms(node: exp.Expression | None) -> tuple[PredicateAtom, ...]:
    if node is None:
        return ()
    root = node.this if isinstance(node, (exp.Where, exp.Having)) else node
    leaves: list[exp.Expression] = []

    def visit(item: exp.Expression) -> None:
        if isinstance(item, (exp.And, exp.Or)):
            visit(item.left)
            visit(item.right)
        else:
            leaves.append(item)

    visit(root)
    atoms: list[PredicateAtom] = []
    for item in leaves:
        if _is_identity_where(item):
            continue
        fields = tuple(sorted({column.name.lower() for column in item.find_all(exp.Column)}))
        literals = list(item.find_all(exp.Literal))
        exact = tuple(sorted(f"{_literal_category(value)}:{str(value.this).lower()}" for value in literals))
        categories = tuple(sorted(_literal_category(value) for value in literals))
        parameterized = any(
            isinstance(candidate, (exp.Parameter, exp.Placeholder, exp.Var))
            for candidate in item.walk()
        )
        atoms.append(
            PredicateAtom(
                fields=fields,
                operator=type(item).__name__.upper(),
                exact_values=exact,
                value_categories=categories,
                parameterized=parameterized,
            )
        )
    return tuple(sorted(atoms, key=lambda atom: (atom.field_key, atom.operator_key, atom.exact_values)))


def _boolean_structure(node: exp.Expression | None) -> tuple[str, ...]:
    if node is None:
        return ()
    root = node.this if isinstance(node, (exp.Where, exp.Having)) else node
    return tuple(
        sorted(type(item).__name__.upper() for item in root.walk() if isinstance(item, (exp.And, exp.Or, exp.Not)))
    )


class SQLLogicComparator:
    def build_fingerprint(self, asset: AssetInput) -> SQLFingerprint:
        return self.build_fingerprint_from_sql(asset.sql_text, asset.sql_dialect)

    def build_fingerprint_from_sql(
        self,
        sql_text: str,
        sql_dialect: str = "spark",
    ) -> SQLFingerprint:
        try:
            root = unwrap_identity_layers(parse_one(sql_text, read=sql_dialect))
        except (ParseError, ValueError) as exc:
            return SQLFingerprint(parse_success=False, warning=str(exc))

        cte_names = {
            cte.alias_or_name.lower()
            for cte in root.find_all(exp.CTE)
            if cte.alias_or_name
        }
        input_tables = tuple(
            sorted(
                {
                    table.name.lower()
                    for table in root.find_all(exp.Table)
                    if table.name and table.name.lower() not in cte_names
                }
            )
        )
        columns = tuple(
            sorted(
                {
                    column.name.lower()
                    for column in root.find_all(exp.Column)
                    if column.name and column.name != "*"
                }
            )
        )
        outer_select = root if isinstance(root, exp.Select) else root.find(exp.Select)
        projections = (
            tuple(sorted(canonical_expression(item) for item in outer_select.expressions))
            if outer_select is not None
            else ()
        )
        joins = tuple(
            sorted(
                f"{str(join.args.get('side') or '').lower()}:{str(join.args.get('kind') or 'inner').lower()}:"
                f"{canonical_expression(join.this)}:{canonical_expression(join.args['on']) if join.args.get('on') else ''}"
                for join in root.find_all(exp.Join)
            )
        )
        where = outer_select.args.get("where") if outer_select is not None else None
        having = outer_select.args.get("having") if outer_select is not None else None
        predicate_atoms = tuple([*_predicate_atoms(where), *_predicate_atoms(having)])
        boolean_structure = tuple([*_boolean_structure(where), *_boolean_structure(having)])
        group = outer_select.args.get("group") if outer_select is not None else None
        group_expressions = (
            tuple(sorted(canonical_expression(item) for item in group.expressions))
            if group is not None
            else ()
        )
        aggregate_expressions = tuple(
            sorted(
                canonical_expression(item)
                for item in (outer_select.find_all(exp.AggFunc) if outer_select is not None else [])
            )
        )
        structure_types = (
            exp.Select,
            exp.Join,
            exp.Where,
            exp.Group,
            exp.Having,
            exp.Union,
            exp.Window,
            exp.CTE,
        )
        structures = tuple(
            sorted(type(item).__name__.upper() for item in root.walk() if isinstance(item, structure_types))
        )
        return SQLFingerprint(
            parse_success=True,
            input_tables=input_tables,
            columns=columns,
            projections=projections,
            joins=joins,
            predicates=predicate_atoms,
            boolean_structure=boolean_structure,
            group_expressions=group_expressions,
            aggregate_expressions=aggregate_expressions,
            structures=structures,
        )

    @staticmethod
    def _predicate_similarity(
        left: SQLFingerprint,
        right: SQLFingerprint,
    ) -> tuple[float | None, dict[str, float | None]]:
        left_fields = [field for atom in left.predicates for field in atom.fields]
        right_fields = [field for atom in right.predicates for field in atom.fields]
        field_score = jaccard(left_fields, right_fields)
        operator_score = jaccard(
            [atom.operator_key for atom in left.predicates],
            [atom.operator_key for atom in right.predicates],
        )
        left_map = {atom.operator_key: atom for atom in left.predicates}
        right_map = {atom.operator_key: atom for atom in right.predicates}
        comparable_values: list[float] = []
        for key in sorted(set(left_map) & set(right_map)):
            a = left_map[key]
            b = right_map[key]
            if a.parameterized or b.parameterized:
                continue
            if not a.exact_values and not b.exact_values:
                continue
            if a.exact_values == b.exact_values:
                comparable_values.append(1.0)
            elif a.value_categories == b.value_categories:
                comparable_values.append(0.5)
            else:
                comparable_values.append(0.0)
        value_score = (
            sum(comparable_values) / len(comparable_values)
            if comparable_values
            else None
        )
        boolean_score = multiset_jaccard(left.boolean_structure, right.boolean_structure)
        components = {
            "field": field_score,
            "operator": operator_score,
            "value": value_score,
            "boolean": boolean_score,
        }
        score, _ = weighted_available(components, PREDICATE_WEIGHTS)
        return score, components

    @staticmethod
    def _group_agg_similarity(
        left: SQLFingerprint,
        right: SQLFingerprint,
    ) -> float | None:
        values = {
            "group": multiset_jaccard(left.group_expressions, right.group_expressions),
            "aggregate": multiset_jaccard(
                left.aggregate_expressions,
                right.aggregate_expressions,
            ),
        }
        score, _ = weighted_available(values, {"group": 0.40, "aggregate": 0.60})
        return score

    @staticmethod
    def identifier_similarity(
        left: SQLFingerprint,
        right: SQLFingerprint,
    ) -> float | None:
        score, _ = weighted_available(
            {
                "table": jaccard(left.input_tables, right.input_tables),
                "column": jaccard(left.columns, right.columns),
            },
            {"table": 0.60, "column": 0.40},
        )
        return score

    def compare_fingerprints(
        self,
        a: SQLFingerprint,
        b: SQLFingerprint,
    ) -> LayerScore:
        if not a.parse_success or not b.parse_success:
            warnings = [
                value
                for value in (
                    f"SQL A解析失败：{a.warning}" if not a.parse_success else None,
                    f"SQL B解析失败：{b.warning}" if not b.parse_success else None,
                )
                if value
            ]
            return LayerScore(available=False, quality=0.0, warnings=warnings)

        predicate_score, predicate_components = self._predicate_similarity(a, b)
        components = {
            "input": jaccard(a.input_tables, b.input_tables),
            "projection": multiset_jaccard(a.projections, b.projections),
            "join": multiset_jaccard(a.joins, b.joins),
            "predicate": predicate_score,
            "group_agg": self._group_agg_similarity(a, b),
            "structure": multiset_jaccard(a.structures, b.structures),
        }
        score, availability = weighted_available(components, LOGIC_WEIGHTS)
        if score is None:
            return LayerScore(
                available=False,
                quality=0.0,
                components=components,
                warnings=["SQL可解析，但未提取到可比较逻辑特征"],
            )
        evidence = [
            f"输入表相似度={components['input']:.3f}" if components["input"] is not None else "输入表不可比较",
            f"投影相似度={components['projection']:.3f}" if components["projection"] is not None else "投影不可比较",
            f"分组聚合相似度={components['group_agg']:.3f}" if components["group_agg"] is not None else "分组聚合不可比较",
            "谓词子项=" + ",".join(
                f"{key}:{value:.3f}" for key, value in predicate_components.items() if value is not None
            ),
        ]
        return LayerScore(
            score=score,
            quality=availability,
            available=True,
            components=components,
            evidence=evidence,
        )

    def compare(self, left: AssetInput, right: AssetInput) -> LayerScore:
        return self.compare_fingerprints(
            self.build_fingerprint(left),
            self.build_fingerprint(right),
        )
