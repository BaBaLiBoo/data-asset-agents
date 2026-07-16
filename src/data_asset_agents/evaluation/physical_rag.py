from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from data_asset_agents.ontology.retrieval import deterministic_embedding
from data_asset_agents.validation import DatabaseCatalog


class PhysicalRAGDocument(BaseModel):
    document_id: str
    document_type: Literal["table", "column", "historical_sql"]
    table: str | None = None
    column: str | None = None
    data_type: str | None = None
    comment: str | None = None
    pk_fk_summary: str | None = None
    profile_summary: str | None = None
    masked_samples: list[str] = Field(default_factory=list)
    raw_sql: str | None = None
    search_text: str
    embedding: list[float] = Field(default_factory=list)
    source_hash: str
    build_id: str


class PhysicalRAGSearchResult(BaseModel):
    document: PhysicalRAGDocument
    score: float
    keyword_score: float
    vector_score: float


class PhysicalRAGIndex:
    """Index of physical metadata/raw SQL that cannot access ontology objects."""

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.documents: list[PhysicalRAGDocument] = []
        self.build_id: str | None = None

    def build(
        self,
        catalog: DatabaseCatalog,
        historical_sql_path: Path | str,
        *,
        profiles: dict[tuple[str, str], dict[str, object]] | None = None,
    ) -> str:
        path = Path(historical_sql_path)
        source = path.read_bytes() if path.exists() else b"[]"
        catalog_bytes = catalog.model_dump_json().encode("utf-8")
        source_hash = hashlib.sha256(catalog_bytes + source).hexdigest()
        build_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_hash))
        documents: list[PhysicalRAGDocument] = []
        for table in catalog.tables:
            foreign_keys = "; ".join(
                f"{','.join(item.columns)}->{item.referred_table}."
                f"{','.join(item.referred_columns)}"
                for item in table.foreign_keys
            )
            table_text = " ".join(
                filter(
                    None,
                    [
                        table.name,
                        table.comment,
                        "columns " + " ".join(column.name for column in table.columns),
                        "primary key " + " ".join(table.primary_key),
                        foreign_keys,
                    ],
                )
            )
            documents.append(
                self._document(
                    build_id,
                    source_hash,
                    "table",
                    table_text,
                    table=table.name,
                    comment=table.comment,
                    pk_fk_summary=foreign_keys or None,
                )
            )
            for column in table.columns:
                profile = (profiles or {}).get((table.name, column.name), {})
                profile_summary = json.dumps(profile, ensure_ascii=False, sort_keys=True)
                column_text = " ".join(
                    filter(
                        None,
                        [
                            table.name,
                            column.name,
                            column.data_type,
                            table.comment,
                            profile_summary if profile else None,
                        ],
                    )
                )
                documents.append(
                    self._document(
                        build_id,
                        source_hash,
                        "column",
                        column_text,
                        table=table.name,
                        column=column.name,
                        data_type=column.data_type,
                        comment=table.comment,
                        profile_summary=profile_summary if profile else None,
                        masked_samples=[str(item) for item in profile.get("samples", [])],
                    )
                )
        raw_records = json.loads(source.decode("utf-8")) if source else []
        for index, record in enumerate(raw_records):
            raw_sql = str(record.get("sql") or "")
            # Deliberately exclude certification, ontology IDs, semantic policy,
            # lifecycle, version and parsed SQLAsset fields.
            search_text = " ".join(
                filter(
                    None,
                    [
                        str(record.get("question") or ""),
                        str(record.get("business_summary") or ""),
                        raw_sql,
                    ],
                )
            )
            documents.append(
                self._document(
                    build_id,
                    source_hash,
                    "historical_sql",
                    search_text,
                    raw_sql=raw_sql,
                    document_id=f"historical-sql-{index:04d}",
                )
            )
        self.documents = documents
        self.build_id = build_id
        return build_id

    def _document(
        self,
        build_id: str,
        source_hash: str,
        document_type: Literal["table", "column", "historical_sql"],
        search_text: str,
        *,
        document_id: str | None = None,
        **fields: object,
    ) -> PhysicalRAGDocument:
        return PhysicalRAGDocument(
            document_id=document_id
            or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{build_id}:{document_type}:{search_text}")),
            document_type=document_type,
            search_text=search_text,
            embedding=deterministic_embedding(search_text, self.dimensions),
            source_hash=source_hash,
            build_id=build_id,
            **fields,
        )

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(
            sum(b * b for b in right)
        )
        return numerator / denominator if denominator else 0.0

    def search(self, query: str, limit: int = 8) -> list[PhysicalRAGSearchResult]:
        query_vector = deterministic_embedding(query, self.dimensions)
        tokens = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", query.lower()))
        results: list[PhysicalRAGSearchResult] = []
        for document in self.documents:
            text = document.search_text.lower()
            keyword = sum(token in text for token in tokens) / max(len(tokens), 1)
            vector = max(0.0, self._cosine(query_vector, document.embedding))
            score = 0.55 * vector + 0.45 * keyword
            results.append(
                PhysicalRAGSearchResult(
                    document=document,
                    score=round(score, 6),
                    keyword_score=round(keyword, 6),
                    vector_score=round(vector, 6),
                )
            )
        return sorted(results, key=lambda item: (-item.score, item.document.document_id))[
            :limit
        ]
