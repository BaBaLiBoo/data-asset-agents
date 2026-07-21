from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError, QueryExecutionError
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.retrieval import deterministic_embedding
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.models import (
    CertificationLevel,
    SQLAsset,
    SQLAssetBuild,
    SQLAssetBuildReport,
    SQLAssetBuildStatus,
    SQLAssetScore,
    SQLAssetSearchRequest,
    SQLAssetSearchResult,
    SQLExecutionStatus,
)
from data_asset_agents.sql_assets.parser import HistoricalSQLParser

LOGGER = logging.getLogger(__name__)


class SQLAssetRepositoryProtocol(Protocol):
    def upsert(self, asset: SQLAsset, embedding: list[float]) -> None: ...
    def get(self, asset_id: str) -> SQLAsset | None: ...
    def list(self, limit: int = 100, offset: int = 0) -> list[SQLAsset]: ...
    def eligible_candidates(
        self,
        embedding: list[float],
        query: str,
        limit: int,
        ontology_version_id: str,
        bundle_hash: str | None = None,
    ) -> list[tuple[SQLAsset, float, float]]: ...
    def create_build(self, build: SQLAssetBuild) -> None: ...
    def finish_build(self, build: SQLAssetBuild) -> None: ...
    def latest_ready(
        self, ontology_version_id: str, bundle_hash: str | None = None
    ) -> SQLAssetBuild | None: ...
    def get_build(self, build_id: str) -> SQLAssetBuild | None: ...


class SQLAssetService:
    """Build, safety-gate, index and rerank certified historical SQL."""

    WEIGHTS = {
        "vector_similarity": 0.25,
        "metric_match": 0.15,
        "dimension_match": 0.10,
        "table_column_coverage": 0.15,
        "join_match": 0.10,
        "ast_similarity": 0.05,
        "certification_score": 0.10,
        "lifecycle_score": 0.10,
    }

    def __init__(
        self,
        repository: SQLAssetRepositoryProtocol,
        ontology: OntologyService,
        executor: ExecutorProtocol,
        settings: Settings | None = None,
        model_factory: ModelFactory | None = None,
        parser: HistoricalSQLParser | None = None,
    ) -> None:
        self.repository = repository
        self.ontology = ontology
        self.executor = executor
        self.settings = settings or Settings()
        self.model_factory = model_factory or ModelFactory(self.settings)
        self.parser = parser or HistoricalSQLParser()

    def _embed_query(self, text: str) -> list[float]:
        if (
            self.settings.llm_mode == "live"
            and self.settings.embedding_api_key.get_secret_value()
        ):
            return self.model_factory.embeddings().embed_query(text)
        return deterministic_embedding(text, self.settings.embedding_dimensions)

    def _embed_documents(self, texts: list[str]) -> list[list[float]]:
        if (
            self.settings.llm_mode == "live"
            and self.settings.embedding_api_key.get_secret_value()
        ):
            return self.model_factory.embeddings().embed_documents(texts)
        return [
            deterministic_embedding(text, self.settings.embedding_dimensions)
            for text in texts
        ]

    def initialize_if_needed(self) -> SQLAssetBuild | None:
        """Load READY state; mock may seed once while live startup never blocks."""

        ready = self.repository.latest_ready(
            self.ontology.ontology_version_id,
            getattr(self.ontology, "compiled_bundle_hash", ""),
        )
        if ready is not None or self.settings.llm_mode != "mock":
            return ready
        try:
            return self.build().build
        except Exception as exc:
            LOGGER.warning(
                "Mock SQL asset initialization failed; continuing without index: %s",
                exc,
            )
            return None

    def build(self, source_path: Path | str | None = None) -> SQLAssetBuildReport:
        path = Path(source_path or self.settings.historical_sql_path)
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise OntologyError(f"Cannot read SQL asset source {path}: {exc}") from exc
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        build = SQLAssetBuild(
            build_id=f"sqlbuild-{uuid4().hex}",
            ontology_version_id=self.ontology.ontology_version_id,
            source_hash=source_hash,
            source_path=path.as_posix(),
            bundle_hash=getattr(self.ontology, "compiled_bundle_hash", ""),
            compiler_version=getattr(self.ontology, "compiler_version", "legacy"),
        )
        self.repository.create_build(build)
        assets: list[SQLAsset] = []
        try:
            assets = self.parser.parse_assets(path, self.ontology)
            for asset in assets:
                asset.ontology_version_id = build.ontology_version_id
                asset.build_id = build.build_id
                asset.source_hash = source_hash
                asset.source_path = build.source_path
                if (
                    asset.certified
                    and asset.parse_error is None
                    and asset.lifecycle_valid
                    and not asset.invalid_columns
                    and not asset.unapproved_joins
                    and asset.semantic_policy_valid
                ):
                    try:
                        plan = self.executor.explain(asset.sql_text, set(asset.tables))
                        asset.execution_status = SQLExecutionStatus.EXPLAIN_PASSED
                        asset.explain_error = None
                        asset.business_summary = asset.business_summary or "；".join(plan[:1])
                    except QueryExecutionError as exc:
                        asset.execution_status = SQLExecutionStatus.EXPLAIN_FAILED
                        asset.explain_error = str(exc)
            vectors = self._embed_documents([asset.retrieval_text for asset in assets])
            indexed_at = datetime.now(UTC)
            for asset, vector in zip(assets, vectors, strict=True):
                asset.indexed_at = indexed_at
                asset.updated_at = indexed_at
                self.repository.upsert(asset, vector)
            eligible = sum(asset.hard_eligible for asset in assets)
            build = build.model_copy(
                update={
                    "status": SQLAssetBuildStatus.READY,
                    "completed_at": datetime.now(UTC),
                    "asset_count": len(assets),
                    "eligible_count": eligible,
                }
            )
            self.repository.finish_build(build)
            return SQLAssetBuildReport(
                build=build,
                parsed=sum(asset.parse_error is None for asset in assets),
                indexed=len(assets),
                eligible=eligible,
                excluded=len(assets) - eligible,
                assets=assets,
            )
        except Exception as exc:
            failed = build.model_copy(
                update={
                    "status": SQLAssetBuildStatus.FAILED,
                    "completed_at": datetime.now(UTC),
                    "error_message": str(exc),
                    "asset_count": len(assets),
                    "eligible_count": 0,
                }
            )
            self.repository.finish_build(failed)
            if isinstance(exc, OntologyError):
                raise
            raise OntologyError(f"SQL asset build {build.build_id} failed: {exc}") from exc

    def get(self, asset_id: str) -> SQLAsset | None:
        return self.repository.get(asset_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[SQLAsset]:
        return self.repository.list(limit, offset)

    @staticmethod
    def _ratio(required: set[str], actual: set[str]) -> float:
        if not required:
            return 1.0
        return len(required & actual) / len(required)

    @staticmethod
    def _join_key(condition: str) -> frozenset[str]:
        return frozenset(part.strip().lower() for part in condition.split("=") if part.strip())

    @staticmethod
    def _target_tags(request: SQLAssetSearchRequest) -> set[str]:
        semantic = request.semantic_query
        tags = {"select"}
        if semantic and semantic.intent == "aggregate":
            tags.add("aggregate")
        if semantic and semantic.dimension_ids:
            tags.add("group_by")
        if request.join_conditions:
            tags.add("join")
        if semantic and semantic.order_by:
            tags.add("order_by")
        if semantic and (semantic.limit or semantic.top_n):
            tags.add("limit")
        if any(term in request.question.lower() for term in ("排名", "排行", "rank")):
            tags.add("window")
        return tags

    def search(self, request: SQLAssetSearchRequest) -> list[SQLAssetSearchResult]:
        expected_bundle_hash = getattr(self.ontology, "compiled_bundle_hash", "")
        if self.repository.latest_ready(
            self.ontology.ontology_version_id, expected_bundle_hash
        ) is None:
            return []
        semantic = request.semantic_query
        metric_ids = set(semantic.metric_ids if semantic else [])
        dimension_ids = set(semantic.dimension_ids if semantic else [])
        required_tables = set(request.selected_tables)
        required_columns = {
            f"{table}.{column}"
            for table, columns in request.selected_columns.items()
            for column in columns
        }
        target_joins = {self._join_key(item) for item in request.join_conditions}
        target_tags = self._target_tags(request)
        candidates = self.repository.eligible_candidates(
            self._embed_query(request.question),
            request.question,
            request.limit * 5,
            self.ontology.ontology_version_id,
            expected_bundle_hash,
        )
        results: list[SQLAssetSearchResult] = []
        for asset, vector_score, keyword_score in candidates:
            asset_columns = {
                f"{column.table}.{column.column}" if column.table else column.column
                for column in asset.columns
            }
            table_score = self._ratio(required_tables, set(asset.tables))
            column_score = self._ratio(required_columns, asset_columns)
            asset_joins = {self._join_key(item.expression) for item in asset.joins}
            ast_tags = set(asset.structural_tags)
            certification = {
                CertificationLevel.GOLD: 1.0,
                CertificationLevel.CERTIFIED: 0.9,
                CertificationLevel.REVIEWED: 0.7,
                CertificationLevel.NONE: 0.0,
            }[asset.certification_level]
            components = {
                "vector_similarity": max(0.0, min(1.0, max(vector_score, keyword_score))),
                "metric_match": self._ratio(metric_ids, set(asset.metrics)),
                "dimension_match": self._ratio(dimension_ids, set(asset.dimensions)),
                "table_column_coverage": (table_score + column_score) / 2,
                "join_match": self._ratio(target_joins, asset_joins),
                "ast_similarity": (
                    len(target_tags & ast_tags) / len(target_tags | ast_tags)
                    if target_tags | ast_tags
                    else 1.0
                ),
                "certification_score": certification,
                "lifecycle_score": 1.0,
            }
            total = sum(
                components[name] * weight for name, weight in self.WEIGHTS.items()
            )
            score = SQLAssetScore(
                **{name: round(value, 6) for name, value in components.items()},
                total=round(total, 6),
            )
            evidence = [
                "通过认证、SQLGlot、生命周期、字段、Join 和 PostgreSQL EXPLAIN 硬门槛",
                f"指标覆盖 {score.metric_match:.0%}，维度覆盖 {score.dimension_match:.0%}",
                f"表字段覆盖 {score.table_column_coverage:.0%}，Join 覆盖 {score.join_match:.0%}",
                f"AST 标签：{', '.join(asset.structural_tags)}",
            ]
            results.append(SQLAssetSearchResult(asset=asset, score=score, evidence=evidence))
        return sorted(results, key=lambda item: (-item.score.total, item.asset.id))[
            : request.limit
        ]
