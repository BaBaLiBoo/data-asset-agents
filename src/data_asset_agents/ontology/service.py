from __future__ import annotations

import hashlib
import re

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import (
    BusinessConcept,
    Dimension,
    Metric,
    OntologyBundle,
    PhysicalMapping,
    TableAsset,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.repository.postgres_repository import (
    PostgresOntologyRepository,
)
from data_asset_agents.ontology.retrieval import (
    HybridConceptRetriever,
    deterministic_embedding,
    published_documents,
)
from data_asset_agents.text2sql.models import (
    MatchedConcept,
    QueryFilter,
    RejectedTable,
    SemanticQuery,
)
from data_asset_agents.text2sql.semantic_parser import SemanticQueryParser


class OntologyService:
    """Published-ontology parsing, concept retrieval and physical resolution."""

    def __init__(
        self,
        repository: YamlOntologyRepository,
        runtime_repository: PostgresOntologyRepository | None = None,
        settings: Settings | None = None,
        model_factory: ModelFactory | None = None,
        bundle_override: OntologyBundle | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_repository = runtime_repository
        self.settings = settings or Settings()
        self.model_factory = model_factory or ModelFactory(self.settings)
        self.seed_bundle: OntologyBundle = repository.load()
        self.bundle: OntologyBundle = bundle_override or (
            runtime_repository.load_latest_published_bundle()
            if runtime_repository is not None
            else None
        ) or self.seed_bundle
        current_version = (
            runtime_repository.get_current_version()
            if runtime_repository is not None
            and hasattr(runtime_repository, "get_current_version")
            else None
        )
        self.ontology_version_id = (
            current_version.id
            if current_version is not None and bundle_override is None
            else self._seed_version_id(self.bundle)
        )
        self.semantic_parser = SemanticQueryParser(self.settings, self.model_factory)
        self.retriever = HybridConceptRetriever(self.settings.embedding_dimensions)
        self._refresh_indexes()

    @staticmethod
    def _seed_version_id(bundle: OntologyBundle) -> str:
        payload = bundle.model_dump_json(exclude_none=True)
        return "yaml-seed-" + hashlib.sha256(payload.encode()).hexdigest()[:20]

    def _refresh_indexes(self) -> None:
        self.mappings_by_concept_id = {
            mapping.concept_id: mapping for mapping in self.bundle.mappings
        }
        resolved_metrics = [self._resolve_metric(metric) for metric in self.bundle.metrics]
        resolved_dimensions = [
            self._resolve_dimension(dimension) for dimension in self.bundle.dimensions
        ]
        self.metrics_by_name = {metric.name: metric for metric in resolved_metrics}
        self.metrics_by_id = {metric.id: metric for metric in resolved_metrics}
        self.dimensions_by_name = {
            dimension.name: dimension for dimension in resolved_dimensions
        }
        self.dimensions_by_id = {
            dimension.id: dimension for dimension in resolved_dimensions
        }
        self.tables_by_name = {table.name: table for table in self.bundle.tables}

    @staticmethod
    def _compile_expression(metric: Metric, mapping: PhysicalMapping) -> str:
        expression = metric.expression
        placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", expression))
        for role in placeholders:
            physical = mapping.column_for(role)
            if physical is None:
                raise OntologyError(
                    f"Metric {metric.id} is missing physical binding for role {role}"
                )
            expression = expression.replace(f"{{{role}}}", f"{mapping.table}.{physical}")
        if not placeholders:
            expression = expression.replace(f"{metric.base_table}.", f"{mapping.table}.")
            for semantic_column, physical_column in mapping.column_bindings.items():
                expression = expression.replace(
                    f"{mapping.table}.{semantic_column}",
                    f"{mapping.table}.{physical_column}",
                )
        return expression

    def _resolve_metric(self, metric: Metric) -> Metric:
        mapping = self.mappings_by_concept_id.get(f"metric:{metric.id}")
        if mapping is None:
            return metric
        required_filters: dict[str, str] = {}
        for semantic_role, value in metric.required_filters.items():
            physical = mapping.column_for(semantic_role)
            if physical is None:
                raise OntologyError(
                    f"Metric {metric.id} is missing filter binding for {semantic_role}"
                )
            required_filters[physical] = value
        return metric.model_copy(
            update={
                "base_table": mapping.table,
                "expression": self._compile_expression(metric, mapping),
                "required_filters": required_filters,
            }
        )

    def _resolve_dimension(self, dimension: Dimension) -> Dimension:
        mapping = self.mappings_by_concept_id.get(f"dimension:{dimension.id}")
        if mapping is None:
            return dimension
        column = mapping.column_for("value")
        if column is None:
            raise OntologyError(
                f"Dimension {dimension.id} must define one explicit value binding"
            )
        return dimension.model_copy(update={"table": mapping.table, "column": column})

    def reload_published(self) -> bool:
        if self.runtime_repository is None:
            return False
        published = self.runtime_repository.load_latest_published_bundle()
        if published is None:
            return False
        self.bundle = published
        current = self.runtime_repository.get_current_version()
        self.ontology_version_id = (
            current.id if current is not None else self._seed_version_id(published)
        )
        self._refresh_indexes()
        return True

    def reload_version(self, version: str) -> bool:
        if self.runtime_repository is None:
            return False
        published = self.runtime_repository.load_published_bundle(version)
        if published is None:
            return False
        self.bundle = published
        version_record = next(
            (
                item
                for item in self.runtime_repository.list_versions()
                if item.version == version
            ),
            None,
        )
        self.ontology_version_id = (
            version_record.id
            if version_record is not None
            else self._seed_version_id(published)
        )
        self._refresh_indexes()
        return True

    def parse(
        self,
        question: str,
        matched_concepts: list[MatchedConcept] | None = None,
    ) -> SemanticQuery:
        allowed = {item.id for item in matched_concepts} if matched_concepts else None
        return self.semantic_parser.parse(question, self.bundle, allowed)

    def search(self, text: str, limit: int = 10) -> list[MatchedConcept]:
        if self.runtime_repository is not None and hasattr(
            self.runtime_repository, "hybrid_search"
        ):
            try:
                results = self.runtime_repository.hybrid_search(
                    text, self._embed_query(text), limit
                )
                if results:
                    return results
            except OntologyError:
                pass
        return self.retriever.search(self.bundle, text, limit)

    def _embed_query(self, text: str) -> list[float]:
        if (
            self.settings.llm_mode == "live"
            and self.settings.embedding_api_key.get_secret_value()
        ):
            return self.model_factory.embeddings().embed_query(text)
        return deterministic_embedding(text, self.settings.embedding_dimensions)

    def rebuild_search_index(self, version_id: str) -> None:
        if self.runtime_repository is None:
            return
        documents = published_documents(self.bundle)
        texts = [document.text for document in documents]
        if (
            self.settings.llm_mode == "live"
            and self.settings.embedding_api_key.get_secret_value()
        ):
            vectors = self.model_factory.embeddings().embed_documents(texts)
        else:
            vectors = [
                deterministic_embedding(text, self.settings.embedding_dimensions)
                for text in texts
            ]
        self.runtime_repository.rebuild_concept_index(
            version_id, self.bundle, vectors
        )

    def get_metrics(self, semantic_query: SemanticQuery) -> list[Metric]:
        try:
            if semantic_query.metric_ids:
                return [self.metrics_by_id[item] for item in semantic_query.metric_ids]
            return [self.metrics_by_name[name] for name in semantic_query.metric_names]
        except KeyError as exc:
            raise OntologyError(f"Unknown published metric: {exc.args[0]}") from exc

    def get_dimensions(self, semantic_query: SemanticQuery) -> list[Dimension]:
        try:
            if semantic_query.dimension_ids:
                return [self.dimensions_by_id[item] for item in semantic_query.dimension_ids]
            return [self.dimensions_by_name[name] for name in semantic_query.dimension_names]
        except KeyError as exc:
            raise OntologyError(f"Unknown published dimension: {exc.args[0]}") from exc

    def _resolve_user_filters(self, semantic_query: SemanticQuery) -> list[QueryFilter]:
        filters: list[QueryFilter] = []
        for semantic_filter in semantic_query.filters:
            mapping = self.mappings_by_concept_id.get(semantic_filter.concept_id)
            if mapping is None and not semantic_filter.concept_id.startswith("dimension:"):
                mapping = self.mappings_by_concept_id.get(
                    f"dimension:{semantic_filter.concept_id}"
                )
            if mapping is None:
                raise OntologyError(
                    f"Unknown published filter concept: {semantic_filter.concept_id}"
                )
            column = mapping.column_for("value")
            if column is None and len(mapping.column_bindings) == 1:
                column = next(iter(mapping.column_bindings.values()))
            if column is None:
                raise OntologyError(
                    f"Filter concept {semantic_filter.concept_id} has no unambiguous binding"
                )
            value = semantic_filter.value
            filters.append(
                QueryFilter(
                    table=mapping.table,
                    field=column,
                    operator=semantic_filter.operator,
                    value=", ".join(value) if isinstance(value, list) else value,
                    source="user",
                )
            )
        return filters

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
                        f"Unknown published time dimension: {metric.time_dimension}"
                    )
                time_dimensions.append(dimension)
        selected = list(
            dict.fromkeys(
                [metric.base_table for metric in metrics]
                + [dimension.table for dimension in dimensions]
                + [dimension.table for dimension in time_dimensions]
            )
        )
        required_columns = {
            column
            for metric in metrics
            for column in re.findall(
                rf"\b{re.escape(metric.base_table)}\.([A-Za-z_][A-Za-z0-9_]*)",
                metric.expression,
            )
        } | {
            column for metric in metrics for column in metric.required_filters
        } | {dimension.column for dimension in [*dimensions, *time_dimensions]}
        selected_assets = [self.tables_by_name[name] for name in selected]

        def normalized_tokens(asset: TableAsset) -> set[str]:
            ignored = {
                "dim", "dwd", "dws", "old", "legacy", "tmp", "test",
                "copy", "result", "day", "month", "deprecated",
            }
            return (set(asset.name.lower().split("_")) | set(asset.tags)) - ignored

        selected_tokens = set().union(
            *(normalized_tokens(asset) for asset in selected_assets)
        )
        related_assets: list[TableAsset] = []
        for asset in self.bundle.tables:
            replacement_related = asset.replacement in selected or any(
                selected_asset.replacement == asset.name
                for selected_asset in selected_assets
            )
            column_related = bool(required_columns & set(asset.columns))
            semantic_related = bool(selected_tokens & normalized_tokens(asset))
            if asset.name in selected or replacement_related or (
                column_related and semantic_related
            ):
                related_assets.append(asset)
        candidates = list(dict.fromkeys(asset.name for asset in related_assets))
        rejected: list[RejectedTable] = []
        for name in candidates:
            asset = self.tables_by_name.get(name)
            if asset and (asset.status != "ACTIVE" or not asset.selectable):
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
            elif asset and name not in selected:
                missing = sorted(required_columns - set(asset.columns))
                rejected.append(
                    RejectedTable(
                        name=name,
                        status=asset.status,
                        replacement=asset.replacement,
                        reason=(
                            "字段覆盖不足：缺少 " + ", ".join(missing)
                            if missing
                            else f"粒度不匹配：{asset.grain}"
                        ),
                    )
                )
        selected = [
            name
            for name in selected
            if self.tables_by_name[name].status == "ACTIVE"
            and self.tables_by_name[name].selectable
        ]
        columns: dict[str, list[str]] = {name: [] for name in selected}
        filters: list[QueryFilter] = self._resolve_user_filters(semantic_query)
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
        for query_filter in filters:
            if query_filter.table in columns:
                columns[query_filter.table].append(query_filter.field)
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
