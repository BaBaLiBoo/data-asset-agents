from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np


SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_cache_v2 (
    cache_key TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    text_sha256 TEXT NOT NULL,
    vector_blob BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embedding_lookup
ON embedding_cache_v2(service_id, model, dimension, text_sha256);
"""


class EmbeddingServiceError(RuntimeError):
    pass


class SQLiteEmbeddingCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _key(service_id: str, model: str, dimension: int, text: str) -> str:
        payload = f"{service_id}\n{model}\n{dimension}\n{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get_many(
        self,
        service_id: str,
        model: str,
        dimension: int,
        texts: list[str],
    ) -> dict[str, np.ndarray]:
        result: dict[str, np.ndarray] = {}
        with self._connect() as connection:
            for text in dict.fromkeys(texts):
                row = connection.execute(
                    """
                    SELECT vector_blob FROM embedding_cache_v2
                    WHERE cache_key = ? AND service_id = ? AND model = ? AND dimension = ?
                    """,
                    (
                        self._key(service_id, model, dimension, text),
                        service_id,
                        model,
                        dimension,
                    ),
                ).fetchone()
                if row is None:
                    self.misses += 1
                    continue
                vector = np.frombuffer(row["vector_blob"], dtype=np.float32).copy()
                if vector.size != dimension:
                    raise EmbeddingServiceError(
                        f"cached vector dimension {vector.size}; expected {dimension}"
                    )
                result[text] = vector
                self.hits += 1
        return result

    def put_many(
        self,
        service_id: str,
        model: str,
        dimension: int,
        vectors: dict[str, np.ndarray],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for text, vector in vectors.items():
            array = np.asarray(vector, dtype=np.float32)
            if array.ndim != 1 or array.size != dimension:
                raise EmbeddingServiceError(
                    f"cannot cache vector dimension {array.size}; expected {dimension}"
                )
            rows.append(
                (
                    self._key(service_id, model, dimension, text),
                    service_id,
                    model,
                    dimension,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    array.tobytes(),
                    now,
                )
            )
        if rows:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO embedding_cache_v2(
                        cache_key, service_id, model, dimension,
                        text_sha256, vector_blob, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self.writes += len(rows)


class OpenAICompatibleEmbeddingClient:
    """OpenAI-compatible online Embedding client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        send_dimensions: bool = True,
        api_key_header: str = "Authorization",
        api_key_prefix: str = "Bearer",
        batch_size: int = 10,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        cache: SQLiteEmbeddingCache | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Embedding API key is required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Embedding base URL must use http:// or https://")
        if not model.strip():
            raise ValueError("Embedding model is required")
        if int(dimension) <= 0:
            raise ValueError("Embedding dimension must be positive")
        if not api_key_header.strip():
            raise ValueError("Embedding API key header cannot be empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.dimension = int(dimension)
        base_url_hash = hashlib.sha256(
            self.base_url.encode("utf-8")
        ).hexdigest()[:12]
        self.service_id = f"openai-compatible:{base_url_hash}"
        self.send_dimensions = bool(send_dimensions)
        self.api_key_header = api_key_header.strip()
        self.api_key_prefix = api_key_prefix.strip()
        self.batch_size = max(1, int(batch_size))
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.cache = cache
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self._memory: dict[str, np.ndarray] = {}
        self.api_requests = 0
        self.api_texts = 0
        self.failed_requests = 0
        self.memory_hits = 0

    @property
    def endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(text.split())

    def embed_many(self, texts: list[str]) -> dict[str, np.ndarray]:
        unique = list(dict.fromkeys(self._clean(text) for text in texts if self._clean(text)))
        self.memory_hits += sum(text in self._memory for text in unique)
        missing = [text for text in unique if text not in self._memory]
        if missing and self.cache is not None:
            cached = self.cache.get_many(
                self.service_id,
                self.model,
                self.dimension,
                missing,
            )
            self._memory.update(cached)
            missing = [text for text in missing if text not in cached]
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            raw_vectors = self._request_batch(batch)
            normalized: dict[str, np.ndarray] = {}
            for text, raw_vector in zip(batch, raw_vectors, strict=True):
                vector = np.asarray(raw_vector, dtype=np.float32)
                if vector.ndim != 1 or vector.size != self.dimension:
                    raise EmbeddingServiceError(
                        f"{self.model} returned dimension {vector.size}; expected {self.dimension}"
                    )
                norm = float(np.linalg.norm(vector))
                if norm == 0.0:
                    raise EmbeddingServiceError("Embedding service returned a zero vector")
                normalized[text] = vector / norm
            self._memory.update(normalized)
            if self.cache is not None:
                self.cache.put_many(
                    self.service_id,
                    self.model,
                    self.dimension,
                    normalized,
                )
        return {text: self._memory[text] for text in unique}

    def cosine(self, left: str, right: str) -> float | None:
        left = self._clean(left)
        right = self._clean(right)
        if not left or not right:
            return None
        vectors = self.embed_many([left, right])
        return max(0.0, min(1.0, float(vectors[left] @ vectors[right])))

    def _request_batch(self, texts: list[str]) -> list[list[float]]:
        self.api_requests += 1
        self.api_texts += len(texts)
        payload: dict[str, object] = {
            "model": self.model,
            "input": texts,
        }
        if self.send_dimensions:
            payload["dimensions"] = self.dimension
        credential = " ".join(
            part for part in (self.api_key_prefix, self.api_key) if part
        )
        headers = {
            self.api_key_header: credential,
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                data = sorted(body["data"], key=lambda item: int(item["index"]))
                if len(data) != len(texts):
                    raise EmbeddingServiceError(
                        f"Embedding service returned {len(data)} vectors for {len(texts)} texts"
                    )
                return [item["embedding"] for item in data]
            except (
                httpx.HTTPError,
                KeyError,
                TypeError,
                ValueError,
                EmbeddingServiceError,
            ) as exc:
                last_error = exc
                retryable = isinstance(exc, httpx.HTTPError) and (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if not retryable or attempt >= self.max_retries:
                    break
                time.sleep(0.5 * (2**attempt))
        self.failed_requests += 1
        raise EmbeddingServiceError(
            f"Online embedding request failed: {last_error}"
        ) from last_error

    def stats(self) -> dict[str, int | float | str]:
        persistent_hits = self.cache.hits if self.cache else 0
        persistent_misses = self.cache.misses if self.cache else 0
        writes = self.cache.writes if self.cache else 0
        total = self.memory_hits + persistent_hits + self.api_texts
        hits = self.memory_hits + persistent_hits
        return {
            "model": self.model,
            "dimension": self.dimension,
            "send_dimensions": self.send_dimensions,
            "memory_hits": self.memory_hits,
            "persistent_hits": persistent_hits,
            "persistent_misses": persistent_misses,
            "writes": writes,
            "api_requests": self.api_requests,
            "api_texts": self.api_texts,
            "failed_requests": self.failed_requests,
            "cache_hit_rate": hits / total if total else 0.0,
        }
