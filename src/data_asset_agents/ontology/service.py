import re
from collections.abc import Iterable

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    BusinessConcept,
    Dimension,
    Metric,
    OntologyBundle,
    TableAsset,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.text2sql.models import (
    MatchedConcept,
    QueryFilter,
    RejectedTable,
    SemanticQuery,
    TimeRange,
)


class OntologyService:
    """Ontology-first parsing, concept lookup, and deterministic physical resolution."""

    def __init__(self, repository: YamlOntologyRepository) -> None:
        self.bundle: OntologyBundle = repository.load()
        self.metrics_by_name = {metric.name: metric for metric in self.bundle.metrics}
        self.dimensions_by_name = {
            dimension.name: dimension for dimension in self.bundle.dimensions
        }
        self.dimensions_by_id = {dimension.id: dimension for dimension in self.bundle.dimensions}
        self.tables_by_name = {table.name: table for table in self.bundle.tables}

    @staticmethod
    def _matches(question: str, name: str, synonyms: Iterable[str]) -> tuple[bool, str]:
        for phrase in sorted([name, *synonyms], key=len, reverse=True):
            if phrase.lower() in question.lower():
                return True, phrase
        return False, ""

    def parse(self, question: str) -> SemanticQuery:
        metric_names: list[str] = []
        dimension_names: list[str] = []
        for metric in self.bundle.metrics:
            matched, _ = self._matches(question, metric.name, metric.synonyms)
            if matched:
                metric_names.append(metric.name)
        credit_context = any(term in question for term in ("信用卡", "贷记卡", "消费"))
        if credit_context and "交易笔数" in metric_names:
            metric_names.remove("交易笔数")
            metric_names.append("信用卡交易笔数")
        if credit_context and "交易金额" in metric_names:
            metric_names.remove("交易金额")
            metric_names.append("信用卡交易金额")
        metric_names = list(dict.fromkeys(metric_names))
        # Prefer the most specific metric when a generic name is a substring.
        if "信用卡交易金额" in metric_names and "交易金额" in metric_names:
            metric_names.remove("交易金额")
        if "信用卡交易笔数" in metric_names and "交易笔数" in metric_names:
            metric_names.remove("交易笔数")
        for dimension in self.bundle.dimensions:
            matched, _ = self._matches(question, dimension.name, dimension.synonyms)
            if matched:
                dimension_names.append(dimension.name)

        days = None
        match = re.search(r"(?:近|最近)\s*(\d+)\s*天", question)
        if match:
            days = int(match.group(1))
        time_range = TimeRange(
            kind="relative_days" if days else "none",
            days=days,
            original_text=match.group(0) if match else None,
        )
        return SemanticQuery(
            metric_names=metric_names,
            dimension_names=dimension_names,
            time_range=time_range,
            intent="aggregate" if metric_names else "unknown",
        )

    def search(self, text: str, limit: int = 10) -> list[MatchedConcept]:
        results: list[MatchedConcept] = []
        searchable: list[tuple[str, str, str, list[str]]] = []
        searchable.extend((c.id, c.name, c.kind, c.synonyms) for c in self.bundle.concepts)
        searchable.extend((m.id, m.name, "metric", m.synonyms) for m in self.bundle.metrics)
        searchable.extend((d.id, d.name, "dimension", d.synonyms) for d in self.bundle.dimensions)
        seen: set[str] = set()
        for concept_id, name, kind, synonyms in searchable:
            identity = f"{kind}:{concept_id}"
            if identity in seen:
                continue
            matched, phrase = self._matches(text, name, synonyms)
            if matched:
                seen.add(identity)
                score = 1.0 if phrase == name else 0.95
                results.append(
                    MatchedConcept(
                        id=concept_id,
                        name=name,
                        kind=kind,
                        matched_text=phrase,
                        score=score,
                    )
                )
        return sorted(results, key=lambda item: item.score, reverse=True)[:limit]

    def get_metrics(self, semantic_query: SemanticQuery) -> list[Metric]:
        try:
            return [self.metrics_by_name[name] for name in semantic_query.metric_names]
        except KeyError as exc:
            raise OntologyError(f"Unknown reviewed metric: {exc.args[0]}") from exc

    def get_dimensions(self, semantic_query: SemanticQuery) -> list[Dimension]:
        try:
            return [self.dimensions_by_name[name] for name in semantic_query.dimension_names]
        except KeyError as exc:
            raise OntologyError(f"Unknown reviewed dimension: {exc.args[0]}") from exc

    def resolve(self, semantic_query: SemanticQuery) -> dict[str, object]:
        metrics = self.get_metrics(semantic_query)
        dimensions = self.get_dimensions(semantic_query)
        time_dimensions: list[Dimension] = []
        if semantic_query.time_range.kind != "none":
            for metric in metrics:
                if not metric.time_dimension:
                    continue
                dimension = self.dimensions_by_id.get(metric.time_dimension)
                if dimension is None:
                    raise OntologyError(
                        f"Unknown reviewed time dimension: {metric.time_dimension}"
                    )
                time_dimensions.append(dimension)
        selected = list(
            dict.fromkeys(
                [metric.base_table for metric in metrics]
                + [dimension.table for dimension in dimensions]
                + [dimension.table for dimension in time_dimensions]
            )
        )

        # Similar-looking distractors are surfaced for auditable lifecycle rejection.
        candidate_names = list(selected)
        if any("transaction" in name for name in selected):
            candidate_names.extend(
                [
                    "dws_branch_transaction_day",
                    "legacy_card_transaction",
                    "tmp_transaction_result",
                    "test_transaction_copy",
                ]
            )
        candidates = list(dict.fromkeys(candidate_names))
        rejected: list[RejectedTable] = []
        for name in candidates:
            asset = self.tables_by_name.get(name)
            if asset and not asset.selectable:
                rejected.append(
                    RejectedTable(
                        name=name,
                        status=asset.status,
                        replacement=asset.replacement,
                        reason=(
                            f"{asset.status}: selectable=false"
                            + (f"; use {asset.replacement}" if asset.replacement else "")
                        ),
                    )
                )
            elif asset and "no_distinct_transaction_id" in asset.tags and any(
                "COUNT(DISTINCT" in metric.expression for metric in metrics
            ):
                rejected.append(
                    RejectedTable(
                        name=name,
                        status=asset.status,
                        reason="粒度不匹配：汇总表不保留 transaction_id，无法支持交易 ID 去重计数",
                    )
                )
        selected = [name for name in selected if self.tables_by_name[name].selectable]
        columns: dict[str, list[str]] = {name: [] for name in selected}
        filters: list[QueryFilter] = []
        for metric in metrics:
            expression_columns = set(
                re.findall(
                    rf"\b{re.escape(metric.base_table)}\.([A-Za-z_][A-Za-z0-9_]*)",
                    metric.expression,
                )
            )
            for column in self.tables_by_name[metric.base_table].columns:
                if column in expression_columns or column in metric.required_filters:
                    columns[metric.base_table].append(column)
            filters.extend(
                QueryFilter(
                    table=metric.base_table,
                    field=field,
                    value=value,
                    source="metric_policy",
                )
                for field, value in metric.required_filters.items()
            )
        for dimension in dimensions:
            columns[dimension.table].append(dimension.column)
        for dimension in time_dimensions:
            columns[dimension.table].append(dimension.column)
        for table in columns:
            columns[table] = list(dict.fromkeys(columns[table]))
        return {
            "metrics": metrics,
            "dimensions": dimensions,
            "candidate_tables": candidates,
            "selected_tables": selected,
            "selected_columns": columns,
            "rejected_tables": rejected,
            "filters": filters,
        }

    def concepts(self) -> list[BusinessConcept]:
        return self.bundle.concepts

    def tables(self) -> list[TableAsset]:
        return self.bundle.tables
