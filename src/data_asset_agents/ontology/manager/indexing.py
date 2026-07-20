"""Versioned ontology search index builds with atomic READY switching."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, text

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.ontology.retrieval import deterministic_embedding, published_documents
from data_asset_agents.text2sql.models import MatchedConcept

from .governance_models import (
    OntologyIndexBuild,
    OntologyIndexBuildRequest,
    OntologyIndexStatus,
    OntologyIndexType,
)
from .repository import OntologyManagerRepository


class OntologyIndexService:
    def __init__(
        self,
        engine: Engine | None,
        repository: OntologyManagerRepository,
        settings: Settings,
        model_factory: ModelFactory,
        bundle_provider: Callable[[], OntologyBundle],
        version_provider: Callable[[], str],
    ) -> None:
        self.engine = engine
        self.repository = repository
        self.settings = settings
        self.model_factory = model_factory
        self.bundle_provider = bundle_provider
        self.version_provider = version_provider
        self._builds: dict[str, OntologyIndexBuild] = {}
        self._documents: dict[str, list[dict[str, object]]] = {}

    def _source_documents(
        self, index_type: OntologyIndexType, bundle: OntologyBundle
    ) -> list[dict[str, object]]:
        if index_type == OntologyIndexType.BUSINESS_CONCEPT:
            return [
                {
                    "resource_type": item.kind.upper(),
                    "resource_id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "synonyms": item.synonyms,
                    "search_text": item.text,
                }
                for item in published_documents(bundle)
            ]
        version_id = self.version_provider()
        _, resources = self.repository.published_resources(version_id)
        groups = {
            OntologyIndexType.OBJECT_TYPE: resources.object_types,
            OntologyIndexType.PROPERTY: resources.properties,
            OntologyIndexType.LINK_TYPE: resources.link_types,
        }
        return [
            {
                "resource_type": index_type.value,
                "resource_id": item.id,
                "name": item.name,
                "description": item.description,
                "synonyms": getattr(item, "synonyms", []),
                "search_text": " ".join(
                    [item.name, item.description, *getattr(item, "synonyms", [])]
                ),
            }
            for item in groups[index_type]
        ]

    @staticmethod
    def _fixed_vector(vector: list[float]) -> list[float]:
        return (vector + [0.0] * 1024)[:1024]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if (
            self.settings.llm_mode == "live"
            and self.settings.embedding_api_key.get_secret_value()
        ):
            return [
                self._fixed_vector(item)
                for item in self.model_factory.embeddings().embed_documents(texts)
            ]
        return [self._fixed_vector(deterministic_embedding(item, 1024)) for item in texts]

    def build(self, request: OntologyIndexBuildRequest) -> OntologyIndexBuild:
        bundle = self.bundle_provider()
        version_id = self.version_provider()
        documents = self._source_documents(request.index_type, bundle)
        source = json.dumps(documents, ensure_ascii=False, sort_keys=True)
        build = OntologyIndexBuild(
            build_id=f"ontology-index-{uuid4().hex}",
            ontology_version_id=version_id,
            index_type=request.index_type,
            status=OntologyIndexStatus.BUILDING,
            source_hash=hashlib.sha256(source.encode()).hexdigest(),
            embedding_model=(
                self.settings.embedding_model
                if self.settings.llm_mode == "live"
                else "deterministic-cpu-v1"
            ),
            embedding_dimensions=1024,
        )
        self._builds[build.build_id] = build
        self._persist_build(build)
        try:
            if not documents:
                raise ValueError("Ontology index source contains no documents")
            vectors = self._embed([str(item["search_text"]) for item in documents])
            self._write_documents(build, documents, vectors)
            build.status = OntologyIndexStatus.READY
            build.document_count = len(documents)
            build.completed_at = datetime.now(UTC)
            build.is_current = True
            self._atomic_switch(build)
            self._documents[build.build_id] = documents
        except Exception as exc:
            build.status = OntologyIndexStatus.FAILED
            build.completed_at = datetime.now(UTC)
            build.error_message = str(exc)
            build.is_current = False
            self._persist_build(build)
        self._builds[build.build_id] = build
        return build

    def _write_documents(
        self,
        build: OntologyIndexBuild,
        documents: list[dict[str, object]],
        vectors: list[list[float]],
    ) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            for document, vector in zip(documents, vectors, strict=True):
                connection.execute(
                    text("""
                    INSERT INTO ontology_search_document(
                      ontology_version_id,build_id,resource_type,resource_id,name,
                      description,synonyms,search_text,embedding
                    ) VALUES (
                      :version_id,:build_id,:resource_type,:resource_id,:name,
                      :description,CAST(:synonyms AS jsonb),:search_text,
                      CAST(:embedding AS vector)
                    )
                    """),
                    {
                        "version_id": build.ontology_version_id,
                        "build_id": build.build_id,
                        **document,
                        "synonyms": json.dumps(document["synonyms"], ensure_ascii=False),
                        "embedding": str(vector),
                    },
                )

    def _atomic_switch(self, build: OntologyIndexBuild) -> None:
        if self.engine is None:
            for item in self._builds.values():
                if (
                    item.ontology_version_id == build.ontology_version_id
                    and item.index_type == build.index_type
                ):
                    item.is_current = item.build_id == build.build_id
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE ontology_index_build SET is_current=false
                WHERE ontology_version_id=:version_id AND index_type=:index_type
                """),
                {
                    "version_id": build.ontology_version_id,
                    "index_type": build.index_type.value,
                },
            )
            connection.execute(
                text("""
                UPDATE ontology_index_build SET
                  status='READY',document_count=:count,completed_at=:completed_at,
                  error_message=NULL,is_current=true
                WHERE build_id=:build_id
                """),
                {
                    "build_id": build.build_id,
                    "count": build.document_count,
                    "completed_at": build.completed_at,
                },
            )

    def _persist_build(self, build: OntologyIndexBuild) -> None:
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO ontology_index_build(
                  build_id,ontology_version_id,index_type,status,source_hash,
                  embedding_model,embedding_dimensions,document_count,started_at,
                  completed_at,error_message,is_current
                ) VALUES (
                  :build_id,:version_id,:index_type,:status,:source_hash,
                  :embedding_model,:dimensions,:count,:started_at,
                  :completed_at,:error_message,:is_current
                ) ON CONFLICT(build_id) DO UPDATE SET
                  status=EXCLUDED.status,document_count=EXCLUDED.document_count,
                  completed_at=EXCLUDED.completed_at,error_message=EXCLUDED.error_message,
                  is_current=EXCLUDED.is_current
                """),
                {
                    "build_id": build.build_id,
                    "version_id": build.ontology_version_id,
                    "index_type": build.index_type.value,
                    "status": build.status.value,
                    "source_hash": build.source_hash,
                    "embedding_model": build.embedding_model,
                    "dimensions": build.embedding_dimensions,
                    "count": build.document_count,
                    "started_at": build.started_at,
                    "completed_at": build.completed_at,
                    "error_message": build.error_message,
                    "is_current": build.is_current,
                },
            )

    def list(self) -> list[OntologyIndexBuild]:
        if self.engine is None:
            return sorted(self._builds.values(), key=lambda item: item.started_at, reverse=True)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT * FROM ontology_index_build ORDER BY started_at DESC")
            ).mappings()
            return [self._row(row) for row in rows]

    def get(self, build_id: str) -> OntologyIndexBuild | None:
        return next((item for item in self.list() if item.build_id == build_id), None)

    def current(self, version_id: str, index_type: OntologyIndexType) -> OntologyIndexBuild | None:
        return next(
            (
                item
                for item in self.list()
                if item.ontology_version_id == version_id
                and item.index_type == index_type
                and item.status == OntologyIndexStatus.READY
                and item.is_current
            ),
            None,
        )

    def search(self, query: str, limit: int = 10) -> list[MatchedConcept]:
        version_id = self.version_provider()
        current = self.current(version_id, OntologyIndexType.BUSINESS_CONCEPT)
        if current is None or self.engine is None:
            return []
        try:
            vector = self._embed([query])[0]
        except Exception:
            return []
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("""
                SELECT resource_id,name,resource_type,synonyms,
                       1 - (embedding <=> CAST(:embedding AS vector)) AS vector_score,
                       ts_rank_cd(to_tsvector('simple', search_text),
                                  plainto_tsquery('simple', :query)) AS keyword_score
                FROM ontology_search_document
                WHERE build_id=:build_id
                ORDER BY (0.65 * (1 - (embedding <=> CAST(:embedding AS vector))) +
                          0.35 * ts_rank_cd(to_tsvector('simple', search_text),
                                           plainto_tsquery('simple', :query))) DESC
                LIMIT :limit
                """),
                {
                    "embedding": str(vector),
                    "query": query,
                    "build_id": current.build_id,
                    "limit": limit,
                },
            ).mappings()
            return [
                MatchedConcept(
                    id=str(row["resource_id"]),
                    name=str(row["name"]),
                    kind=str(row["resource_type"]).lower(),
                    matched_text=str(row["name"]),
                    score=max(0.0, float(row["vector_score"] or 0)),
                    evidence=[f"versioned index build: {current.build_id}"],
                    keyword_score=float(row["keyword_score"] or 0),
                    vector_score=float(row["vector_score"] or 0),
                )
                for row in rows
            ]

    @staticmethod
    def _row(row: object) -> OntologyIndexBuild:
        return OntologyIndexBuild(
            build_id=row["build_id"],
            ontology_version_id=row["ontology_version_id"],
            index_type=row["index_type"],
            status=row["status"],
            source_hash=row["source_hash"],
            embedding_model=row["embedding_model"],
            embedding_dimensions=row["embedding_dimensions"],
            document_count=row["document_count"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error_message=row["error_message"],
            is_current=row["is_current"],
        )
