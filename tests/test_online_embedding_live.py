from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.embedding import OpenAICompatibleEmbeddingClient, SQLiteEmbeddingCache
from app.models import AssetInput
from app.semantic import SemanticComparator
from tests.test_models import sample_asset


@pytest.mark.live
def test_real_online_embedding_dimension_cosine_semantic_and_cache(tmp_path: Path) -> None:
    settings = Settings.from_env()
    settings.require_embedding()
    cache = SQLiteEmbeddingCache(tmp_path / "embedding_cache.db")
    embedding_client = OpenAICompatibleEmbeddingClient(
        api_key=settings.embedding_api_key or "",
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        send_dimensions=settings.embedding_send_dimensions,
        api_key_header=settings.embedding_api_key_header,
        api_key_prefix=settings.embedding_api_key_prefix,
        batch_size=settings.embedding_batch_size,
        timeout_seconds=settings.embedding_timeout_seconds,
        max_retries=settings.embedding_max_retries,
        cache=cache,
    )
    texts = ["个人客户月日均存款", "自然人客户月平均存款余额"]
    vectors = embedding_client.embed_many(texts)
    expected_shape = (settings.embedding_dimension,)
    assert vectors[texts[0]].shape == expected_shape
    assert vectors[texts[1]].shape == expected_shape
    assert np.linalg.norm(vectors[texts[0]]) == pytest.approx(1.0, abs=1e-5)
    cosine = embedding_client.cosine(*texts)
    assert cosine is not None and 0.0 <= cosine <= 1.0

    left = AssetInput.model_validate(sample_asset())
    right_payload = sample_asset()
    right_payload.update(
        {
            "asset_id": "A002",
            "asset_name": "自然人客户月平均存款余额",
            "description": "按自然人客户和月份计算人民币储蓄平均余额",
        }
    )
    semantic = SemanticComparator(embedding_client).compare(
        left,
        AssetInput.model_validate(right_payload),
    )
    assert semantic.available is True
    assert semantic.score is not None and 0.0 <= semantic.score <= 1.0
    assert embedding_client.model == settings.embedding_model
    assert embedding_client.dimension == settings.embedding_dimension
    assert embedding_client.failed_requests == 0

    second_cache = SQLiteEmbeddingCache(tmp_path / "embedding_cache.db")
    second_client = OpenAICompatibleEmbeddingClient(
        api_key=settings.embedding_api_key or "",
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        send_dimensions=settings.embedding_send_dimensions,
        api_key_header=settings.embedding_api_key_header,
        api_key_prefix=settings.embedding_api_key_prefix,
        cache=second_cache,
    )
    second_client.embed_many(texts)
    assert second_cache.hits == 2
    assert second_client.api_requests == 0


class StubResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {
                    "index": 0,
                    "embedding": [1.0, 2.0, 3.0],
                }
            ]
        }


class StubClient:
    def __init__(self) -> None:
        self.last_json: dict | None = None
        self.last_headers: dict | None = None

    def post(self, url, *, json, headers, timeout):
        self.last_json = json
        self.last_headers = headers
        return StubResponse()


def test_client_accepts_configurable_model_dimension_and_payload() -> None:
    client = StubClient()
    embedding_client = OpenAICompatibleEmbeddingClient(
        api_key="test-key",
        base_url="https://embedding.example/v1",
        model="custom-embedding-model",
        dimension=3,
        send_dimensions=False,
        api_key_header="api-key",
        api_key_prefix="",
        client=client,
    )

    vectors = embedding_client.embed_many(["测试文本"])

    assert vectors["测试文本"].shape == (3,)
    assert embedding_client.model == "custom-embedding-model"
    assert embedding_client.dimension == 3
    assert client.last_json == {
        "model": "custom-embedding-model",
        "input": ["测试文本"],
    }
    assert client.last_headers is not None
    assert client.last_headers["api-key"] == "test-key"
