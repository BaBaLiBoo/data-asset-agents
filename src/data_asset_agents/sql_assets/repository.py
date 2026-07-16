from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.sql_assets.models import SQLAsset
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
    """Version-independent certified SQL asset store backed by pgvector."""

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
                            created_at, updated_at
                        ) VALUES (
                            :asset_id, :question, :business_summary, :certified,
                            :certification_level, :sql_text, :dialect, :metrics,
                            :dimensions, :tables, :ast_fingerprint, :structural_tags,
                            :execution_status, :lifecycle_valid, :parse_valid,
                            :explain_valid, CAST(:payload AS JSONB),
                            CAST(:embedding AS vector),
                            to_tsvector('simple', :search_text), :created_at, :updated_at
                        )
                        ON CONFLICT (asset_id) DO UPDATE SET
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
                    },
                )
        except SQLAlchemyError as exc:
            raise OntologyError(f"Cannot persist SQL asset {asset.id}: {exc}") from exc

    def get(self, asset_id: str) -> SQLAsset | None:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text("SELECT payload FROM sql_asset WHERE asset_id = :asset_id"),
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
                        FROM sql_asset
                        WHERE certified
                          AND lifecycle_valid
                          AND parse_valid
                          AND explain_valid
                          AND execution_status = 'EXPLAIN_PASSED'
                          AND jsonb_array_length(payload->'invalid_columns') = 0
                          AND jsonb_array_length(payload->'unapproved_joins') = 0
                        ORDER BY vector_score DESC, keyword_score DESC, updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "embedding": self._vector_literal(embedding),
                        "query": query,
                        "limit": limit,
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


class MemorySQLAssetRepository:
    """Deterministic repository used by unit tests and database-free consumers."""

    def __init__(self, assets: list[SQLAsset] | None = None) -> None:
        self.assets = {asset.id: asset for asset in assets or []}

    def upsert(self, asset: SQLAsset, embedding: list[float]) -> None:
        self.assets[asset.id] = asset

    def get(self, asset_id: str) -> SQLAsset | None:
        return self.assets.get(asset_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[SQLAsset]:
        return list(self.assets.values())[offset : offset + limit]

    def eligible_candidates(
        self, embedding: list[float], query: str, limit: int
    ) -> list[tuple[SQLAsset, float, float]]:
        from data_asset_agents.ontology.retrieval import deterministic_embedding

        def cosine(left: list[float], right: list[float]) -> float:
            return max(0.0, sum(a * b for a, b in zip(left, right, strict=False)))

        scored: list[tuple[SQLAsset, float, float]] = []
        for asset in self.assets.values():
            if not asset.hard_eligible:
                continue
            vector = deterministic_embedding(asset.retrieval_text, len(embedding))
            keyword = float(bool(set(query.lower()) & set(asset.retrieval_text.lower())))
            scored.append((asset, cosine(embedding, vector), keyword))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]
