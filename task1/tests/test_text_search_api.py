from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.embedding import EmbeddingServiceError
from tests.test_catalog_retrieval import asset, make_service


def test_search_by_text_api_supports_text_and_sql(tmp_path: Path) -> None:
    target = asset("A001")
    service = make_service(
        tmp_path,
        [target, asset("L001", domain="loan")],
    )
    app = create_app(service)
    with TestClient(app) as client:
        text_response = client.post(
            "/api/v1/task1/search-by-text",
            json={"query": target.asset_name},
        )
        sql_response = client.post(
            "/api/v1/task1/search-by-text",
            json={
                "query": target.asset_name,
                "sql_text": target.sql_text,
                "sql_dialect": target.sql_dialect,
            },
        )
    assert text_response.status_code == 200
    assert text_response.json()["retrieval_mode"] == "TEXT_ONLY"
    assert sql_response.status_code == 200
    assert sql_response.json()["retrieval_mode"] == "TEXT_SQL_ENHANCED"
    candidate = sql_response.json()["candidates"][0]
    assert "sql_text" not in candidate
    assert "decision" not in candidate
    assert "fusion_score" not in candidate


def test_search_by_text_api_rejects_blank_query_and_invalid_top_k(
    tmp_path: Path,
) -> None:
    app = create_app(make_service(tmp_path, [asset("A001")]))
    with TestClient(app) as client:
        blank = client.post(
            "/api/v1/task1/search-by-text",
            json={"query": "   "},
        )
        invalid_top_k = client.post(
            "/api/v1/task1/search-by-text",
            json={"query": "存款", "top_k": 21},
        )
    assert blank.status_code == 422
    assert invalid_top_k.status_code == 422


def test_search_by_text_embedding_failure_returns_502(tmp_path: Path) -> None:
    service = make_service(tmp_path, [asset("A001")])

    class FailingEmbedding:
        @staticmethod
        def embed_many(texts: list[str]) -> dict:
            raise EmbeddingServiceError("embedding unavailable")

    assert service.retriever is not None
    service.retriever.embedding_client = FailingEmbedding()
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/task1/search-by-text",
            json={"query": "个人客户月均存款"},
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "embedding unavailable"
