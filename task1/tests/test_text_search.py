from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import (
    TextAssetSearchRequest,
    TextRetrievalMode,
    TextSearchStatus,
)
from app.query_normalizer import QueryNormalizer
from tests.test_catalog_retrieval import asset, make_service


def test_rule_normalizer_extracts_core_business_phrase() -> None:
    result = QueryNormalizer().normalize(
        "我想开发个人客户月日均存款，看看有没有已有资产可以复用"
    )
    assert result.normalized_query == "个人客户月日均存款"
    assert result.method == "RULE_MATCHED"


def test_text_only_search_returns_candidates_without_formal_decision(
    tmp_path: Path,
) -> None:
    service = make_service(
        tmp_path,
        [asset("A001"), asset("L001", domain="loan")],
    )
    result = service.search_by_text(
        TextAssetSearchRequest(
            query="我想开发个人客户月日均存款，看看有没有已有资产可以复用",
            top_k=5,
        )
    )
    assert result.status == TextSearchStatus.MATCHED
    assert result.retrieval_mode == TextRetrievalMode.TEXT_ONLY
    assert result.candidates[0].asset_id == "A001"
    components = result.candidates[0].score_components
    assert result.candidates[0].recall_score == pytest.approx(
        0.80 * components["embedding"]
        + 0.20 * components["keyword"]
    )
    payload = result.model_dump(mode="json")
    assert "decision" not in payload["candidates"][0]
    assert "fusion_score" not in payload["candidates"][0]


def test_valid_domain_filters_and_unknown_domain_falls_back(
    tmp_path: Path,
) -> None:
    service = make_service(
        tmp_path,
        [asset("A001"), asset("L001", domain="loan")],
    )
    filtered = service.search_by_text(
        TextAssetSearchRequest(
            query="贷款账户未偿余额",
            business_domain="loan",
        )
    )
    assert filtered.business_domain_applied == "loan"
    assert filtered.eligible_assets == 1
    assert all(item.business_domain == "loan" for item in filtered.candidates)

    fallback = service.search_by_text(
        TextAssetSearchRequest(
            query="贷款账户未偿余额",
            business_domain="unknown-domain",
        )
    )
    assert fallback.business_domain_applied is None
    assert fallback.eligible_assets == 2
    assert fallback.warnings


def test_sql_enhancement_and_parse_failure_fallback(tmp_path: Path) -> None:
    target = asset("A001")
    service = make_service(
        tmp_path,
        [target, asset("L001", domain="loan")],
    )
    enhanced = service.search_by_text(
        TextAssetSearchRequest(
            query="个人客户月日均存款",
            sql_text=target.sql_text,
            sql_dialect=target.sql_dialect,
        )
    )
    assert enhanced.retrieval_mode == TextRetrievalMode.TEXT_SQL_ENHANCED
    assert enhanced.candidates[0].asset_id == "A001"
    assert "sql_logic" in enhanced.candidates[0].score_components
    components = enhanced.candidates[0].score_components
    assert enhanced.candidates[0].recall_score == pytest.approx(
        0.50 * components["embedding"]
        + 0.10 * components["keyword"]
        + 0.30 * components["sql_logic"]
        + 0.10 * components["sql_identifier"]
    )

    fallback = service.search_by_text(
        TextAssetSearchRequest(
            query="个人客户月日均存款",
            sql_text="SELECT FROM WHERE",
        )
    )
    assert fallback.retrieval_mode == TextRetrievalMode.TEXT_ONLY_FALLBACK
    assert fallback.warnings
    assert service.retriever is not None
    assert fallback.min_score == service.retriever.text_min_score


def test_no_match_returns_http_200_and_empty_candidates(tmp_path: Path) -> None:
    service = make_service(tmp_path, [asset("A001")])
    assert service.retriever is not None
    service.retriever.text_min_score = 1.0
    app = create_app(service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/task1/search-by-text",
            json={
                "query": "完全无关的火星气象观测指标",
                "top_k": 5,
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "NO_MATCH"
    assert response.json()["candidates"] == []
