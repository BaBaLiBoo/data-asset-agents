from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.sql_assets.models import (
    SQLAsset,
    SQLAssetBuild,
    SQLAssetBuildStatus,
)
from data_asset_agents.text2sql.models import HistoricalSQLExample


class HistoricalSQLRepository:
    """Small local certified-SQL repository; pgvector can replace its ranking adapter later."""

    def __init__(self, path: Path | str = "data/historical_sql/examples.json") -> None:
        self.path = Path(path)

    def search(self, question: str, limit: int = 3) -> list[HistoricalSQLExample]:
        if not self.path.exists():
            return []
        records = json.loads(self.path.read_text(encoding="utf-8"))
        question_tokens = set(question.lower())
        examples: list[HistoricalSQLExample] = []
        for record in records:
            overlap = len(question_tokens & set(record["question"].lower()))
            denominator = max(len(question_tokens), 1)
            examples.append(
                HistoricalSQLExample(**record, similarity=round(overlap / denominator, 4))
            )
        return sorted(examples, key=lambda item: item.similarity, reverse=True)[:limit]


class PostgresSQLAssetRepository:
    """Versioned SQL asset builds backed by PostgreSQL and pgvector."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{value:.10f}" for value in vector) + "]"

    def upsert(self, asset: SQLAsset, embedding: list[float]) -> None:
        payload = asset.model_dump(mode="json")
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO sql_asset (
                            asset_id, question, business_summary, certified,
                            certification_level, sql_text, dialect, metrics,
                            dimensions, tables, ast_fingerprint, structural_tags,
                            execution_status, lifecycle_valid, parse_valid,
                            explain_valid, payload, embedding, search_document,
                            created_at, updated_at, ontology_version_id, build_id,
                            source_hash, source_path, indexed_at,
                            semantic_policy_valid, metric_policy_violations
                        ) VALUES (
                            :asset_id, :question, :business_summary, :certified,
                            :certification_level, :sql_text, :dialect, :metrics,
                            :dimensions, :tables, :ast_fingerprint, :structural_tags,
                            :execution_status, :lifecycle_valid, :parse_valid,
                            :explain_valid, CAST(:payload AS JSONB),
                            CAST(:embedding AS vector),
                            to_tsvector('simple', :search_text), :created_at, :updated_at,
                            :ontology_version_id, :build_id, :source_hash, :source_path,
                            :indexed_at, :semantic_policy_valid, :metric_policy_violations
                        )
                        ON CONFLICT (build_id, asset_id) DO UPDATE SET
                            question = EXCLUDED.question,
                            business_summary = EXCLUDED.business_summary,
                            certified = EXCLUDED.certified,
                            certification_level = EXCLUDED.certification_level,
                            sql_text = EXCLUDED.sql_text,
                            dialect = EXCLUDED.dialect,
                            metrics = EXCLUDED.metrics,
                            dimensions = EXCLUDED.dimensions,
                            tables = EXCLUDED.tables,
                            ast_fingerprint = EXCLUDED.ast_fingerprint,
                            structural_tags = EXCLUDED.structural_tags,
                            execution_status = EXCLUDED.execution_status,
                            lifecycle_valid = EXCLUDED.lifecycle_valid,
                            parse_valid = EXCLUDED.parse_valid,
                            explain_valid = EXCLUDED.explain_valid,
                            payload = EXCLUDED.payload,
                            embedding = EXCLUDED.embedding,
                            search_document = EXCLUDED.search_document,
                            semantic_policy_valid = EXCLUDED.semantic_policy_valid,
                            metric_policy_violations = EXCLUDED.metric_policy_violations,
                            indexed_at = EXCLUDED.indexed_at,
                            updated_at = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "asset_id": asset.id,
                        "question": asset.question,
                        "business_summary": asset.business_summary,
                        "certified": asset.certified,
                        "certification_level": asset.certification_level.value,
                        "sql_text": asset.sql_text,
                        "dialect": asset.dialect,
                        "metrics": asset.metrics,
                        "dimensions": asset.dimensions,
                        "tables": asset.tables,
                        "ast_fingerprint": asset.ast_fingerprint,
                        "structural_tags": asset.structural_tags,
                        "execution_status": asset.execution_status.value,
                        "lifecycle_valid": asset.lifecycle_valid,
                        "parse_valid": asset.parse_error is None,
                        "explain_valid": asset.execution_status.value == "EXPLAIN_PASSED",
                        "payload": json.dumps(payload, ensure_ascii=False),
                        "embedding": self._vector_literal(embedding),
                        "search_text": asset.retrieval_text,
                        "created_at": asset.created_at,
                        "updated_at": asset.updated_at,
                        "ontology_version_id": asset.ontology_version_id,
                        "build_id": asset.build_id,
                        "source_hash": asset.source_hash,
                        "source_path": asset.source_path,
                        "indexed_at": asset.indexed_at,
                        "semantic_policy_valid": asset.semantic_policy_valid,
                        "metric_policy_violations": asset.metric_policy_violations,
                    },
                )
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot persist SQL asset {asset.id}: {exc}") from exc

    def get(self, asset_id: str) -> SQLAsset | None:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT payload FROM sql_asset WHERE asset_id = :asset_id "
                        "ORDER BY indexed_at DESC NULLS LAST LIMIT 1"
                    ),
                    {"asset_id": asset_id},
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot read SQL asset {asset_id}: {exc}") from exc
        return SQLAsset.model_validate(row["payload"]) if row else None

    def list(self, limit: int = 100, offset: int = 0) -> list[SQLAsset]:
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT payload FROM sql_asset "
                        "ORDER BY updated_at DESC, asset_id LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": limit, "offset": offset},
                ).mappings()
                return [SQLAsset.model_validate(row["payload"]) for row in rows]
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot list SQL assets: {exc}") from exc

    def eligible_candidates(
        self,
        embedding: list[float],
        query: str,
        limit: int,
        ontology_version_id: str,
        bundle_hash: str | None = None,
    ) -> list[tuple[SQLAsset, float, float]]:
        """Return only assets that pass every hard safety gate."""

        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT payload,
                               GREATEST(0, 1 - (embedding <=> CAST(:embedding AS vector)))
                                   AS vector_score,
                               ts_rank_cd(search_document,
                                   plainto_tsquery('simple', :query)) AS keyword_score
                        FROM sql_asset a
                        JOIN sql_asset_build b ON b.build_id = a.build_id
                        WHERE certified
                          AND a.lifecycle_valid
                          AND a.parse_valid
                          AND a.explain_valid
                          AND a.semantic_policy_valid
                          AND a.execution_status = 'EXPLAIN_PASSED'
                          AND a.ontology_version_id = :ontology_version_id
                          AND b.status = 'READY'
                          AND (:bundle_hash IS NULL OR b.bundle_hash = :bundle_hash)
                          AND b.build_id = (
                              SELECT latest.build_id FROM sql_asset_build latest
                              WHERE latest.ontology_version_id = :ontology_version_id
                                AND latest.status = 'READY'
                                AND (:bundle_hash IS NULL OR latest.bundle_hash = :bundle_hash)
                              ORDER BY latest.completed_at DESC, latest.started_at DESC
                              LIMIT 1
                          )
                          AND jsonb_array_length(a.payload->'invalid_columns') = 0
                          AND jsonb_array_length(a.payload->'unapproved_joins') = 0
                          AND jsonb_array_length(a.payload->'metric_policy_violations') = 0
                        ORDER BY vector_score DESC, keyword_score DESC, a.updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "embedding": self._vector_literal(embedding),
                        "query": query,
                        "limit": limit,
                        "ontology_version_id": ontology_version_id,
                        "bundle_hash": bundle_hash,
                    },
                ).mappings()
                return [
                    (
                        SQLAsset.model_validate(row["payload"]),
                        float(row["vector_score"] or 0),
                        float(row["keyword_score"] or 0),
                    )
                    for row in rows
                ]
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot search SQL assets: {exc}") from exc

    def create_build(self, build: SQLAssetBuild) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO sql_asset_build (
                            build_id, ontology_version_id, source_hash, source_path,
                            status, started_at, asset_count, eligible_count,
                            bundle_hash, compiler_version
                        ) VALUES (
                            :build_id, :ontology_version_id, :source_hash, :source_path,
                            :status, :started_at, 0, 0, :bundle_hash, :compiler_version
                        )
                        """
                    ),
                    {
                        **build.model_dump(),
                        "status": build.status.value,
                    },
                )
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot create SQL asset build: {exc}") from exc

    def finish_build(self, build: SQLAssetBuild) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE sql_asset_build SET
                            status = :status, completed_at = :completed_at,
                            error_message = :error_message, asset_count = :asset_count,
                            eligible_count = :eligible_count
                        WHERE build_id = :build_id AND status = 'BUILDING'
                        """
                    ),
                    {
                        "build_id": build.build_id,
                        "status": build.status.value,
                        "completed_at": build.completed_at,
                        "error_message": build.error_message,
                        "asset_count": build.asset_count,
                        "eligible_count": build.eligible_count,
                    },
                )
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot finish SQL asset build: {exc}") from exc

    def latest_ready(
        self, ontology_version_id: str, bundle_hash: str | None = None
    ) -> SQLAssetBuild | None:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT * FROM sql_asset_build
                        WHERE ontology_version_id = :ontology_version_id
                          AND status = 'READY'
                          AND (:bundle_hash IS NULL OR bundle_hash = :bundle_hash)
                        ORDER BY completed_at DESC, started_at DESC LIMIT 1
                        """
                    ),
                    {
                        "ontology_version_id": ontology_version_id,
                        "bundle_hash": bundle_hash,
                    },
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot load READY SQL asset build: {exc}") from exc
        return self._build_from_row(row) if row else None

    def get_build(self, build_id: str) -> SQLAssetBuild | None:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text("SELECT * FROM sql_asset_build WHERE build_id = :build_id"),
                    {"build_id": build_id},
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot load SQL asset build: {exc}") from exc
        return self._build_from_row(row) if row else None

    @staticmethod
    def _build_from_row(row: object) -> SQLAssetBuild:
        values = dict(row)  # type: ignore[arg-type]
        return SQLAssetBuild(**values)


class MemorySQLAssetRepository:
    """Deterministic repository used by unit tests and database-free consumers."""

    def __init__(self, assets: list[SQLAsset] | None = None) -> None:
        self.assets = {(asset.build_id, asset.id): asset for asset in assets or []}
        self.builds: dict[str, SQLAssetBuild] = {}

    def upsert(self, asset: SQLAsset, embedding: list[float]) -> None:
        self.assets[(asset.build_id, asset.id)] = asset

    def get(self, asset_id: str) -> SQLAsset | None:
        matches = [asset for (_, item_id), asset in self.assets.items() if item_id == asset_id]
        return max(matches, key=lambda item: item.indexed_at or item.updated_at, default=None)

    def list(self, limit: int = 100, offset: int = 0) -> list[SQLAsset]:
        return sorted(
            self.assets.values(),
            key=lambda item: item.indexed_at or item.updated_at,
            reverse=True,
        )[offset : offset + limit]

    def eligible_candidates(
        self,
        embedding: list[float],
        query: str,
        limit: int,
        ontology_version_id: str,
        bundle_hash: str | None = None,
    ) -> list[tuple[SQLAsset, float, float]]:
        from data_asset_agents.ontology.retrieval import deterministic_embedding

        def cosine(left: list[float], right: list[float]) -> float:
            return max(0.0, sum(a * b for a, b in zip(left, right, strict=False)))

        scored: list[tuple[SQLAsset, float, float]] = []
        ready = self.latest_ready(ontology_version_id, bundle_hash)
        for asset in self.assets.values():
            if (
                not asset.hard_eligible
                or ready is None
                or asset.build_id != ready.build_id
                or asset.ontology_version_id != ontology_version_id
            ):
                continue
            vector = deterministic_embedding(asset.retrieval_text, len(embedding))
            keyword = float(bool(set(query.lower()) & set(asset.retrieval_text.lower())))
            scored.append((asset, cosine(embedding, vector), keyword))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    def create_build(self, build: SQLAssetBuild) -> None:
        self.builds[build.build_id] = build.model_copy(deep=True)

    def finish_build(self, build: SQLAssetBuild) -> None:
        self.builds[build.build_id] = build.model_copy(deep=True)

    def latest_ready(
        self, ontology_version_id: str, bundle_hash: str | None = None
    ) -> SQLAssetBuild | None:
        ready = [
            build
            for build in self.builds.values()
            if build.ontology_version_id == ontology_version_id
            and build.status == SQLAssetBuildStatus.READY
            and (bundle_hash is None or build.bundle_hash == bundle_hash)
        ]
        return max(
            ready,
            key=lambda item: item.completed_at or item.started_at,
            default=None,
        )

    def get_build(self, build_id: str) -> SQLAssetBuild | None:
        return self.builds.get(build_id)
