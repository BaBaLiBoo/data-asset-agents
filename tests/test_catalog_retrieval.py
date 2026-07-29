from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.catalog import SQLiteAssetCatalog
from app.config import Settings
from app.fusion import ThreeLayerFusion
from app.keyword import KeywordComparator
from app.lineage import LineageComparator
from app.models import (
    AssetInput,
    Decision,
    FullScanRequest,
    SearchDuplicatesRequest,
)
from app.retrieval import HybridCandidateRetriever
from app.semantic import SemanticComparator
from app.service import Task1Service
from app.sql_logic import SQLLogicComparator
from tests.test_models import sample_asset


class FakeEmbeddingClient:
    name = "fake:embedding:64"

    def __init__(self) -> None:
        self._memory: dict[str, np.ndarray] = {}

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        normalized = re.sub(r"\s+", "", text.lower())
        grams = [normalized[index : index + 2] for index in range(max(1, len(normalized) - 1))]
        vector = np.zeros(64, dtype=np.float32)
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:2], "big") % 64] += 1.0
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector

    def embed_many(self, texts: list[str]) -> dict[str, np.ndarray]:
        unique = [self._clean(text) for text in dict.fromkeys(texts) if self._clean(text)]
        for text in unique:
            self._memory.setdefault(text, self._vector(text))
        return {text: self._memory[text] for text in unique}


def asset(asset_id: str, *, domain: str = "deposit") -> AssetInput:
    payload = sample_asset()
    payload["asset_id"] = asset_id
    if domain == "loan":
        payload.update(
            {
                "asset_name": "贷款账户未偿余额",
                "description": "按贷款账户统计未偿本金和利息",
                "business_domain": "loan",
                "sql_text": "SELECT loan_id FROM dwd_loan_account",
                "declared_grain": ["loan_account"],
                "tables": [
                    {
                        "name": "dwd_loan_account",
                        "columns": [{"name": "loan_id"}],
                    }
                ],
                "lineage": {
                    "direct_upstreams": ["dwd_loan_account"],
                    "root_sources": ["m_loan"],
                    "coverage": 0.8,
                    "freshness": 1.0,
                    "source_reliability": 0.9,
                },
            }
        )
    elif domain == "accounting":
        payload.update(
            {
                "asset_name": "机构会计分录",
                "description": "按机构统计会计科目分录",
                "business_domain": "accounting",
                "sql_text": "SELECT journal_id FROM dwd_gl_journal",
                "declared_grain": ["office", "journal"],
                "tables": [
                    {
                        "name": "dwd_gl_journal",
                        "columns": [{"name": "journal_id"}],
                    }
                ],
                "lineage": {
                    "direct_upstreams": ["dwd_gl_journal"],
                    "root_sources": ["m_journal_entry"],
                    "coverage": 0.8,
                    "freshness": 1.0,
                    "source_reliability": 0.9,
                },
            }
        )
    return AssetInput.model_validate(payload)


def make_service(tmp_path: Path, assets: list[AssetInput]) -> Task1Service:
    settings = replace(
        Settings(),
        embedding_api_key="test-only",
        asset_catalog_path=tmp_path / "catalog.db",
        auto_bootstrap_catalog=False,
    )
    catalog = SQLiteAssetCatalog(settings.asset_catalog_path)
    catalog.upsert_many(assets)
    embedding_client = FakeEmbeddingClient()
    keyword = KeywordComparator()
    return Task1Service(
        settings=settings,
        keyword=keyword,
        semantic=SemanticComparator(embedding_client),
        logic=SQLLogicComparator(),
        lineage=LineageComparator(),
        fusion=ThreeLayerFusion(),
        threshold_profiles={},
        catalog=catalog,
        retriever=HybridCandidateRetriever(
            embedding_client,
            keyword,
            text_min_score=0.0,
            text_sql_min_score=0.0,
        ),
    )


def test_sqlite_catalog_upsert_get_list_and_count(tmp_path: Path) -> None:
    catalog = SQLiteAssetCatalog(tmp_path / "catalog.db")
    catalog.upsert_many([asset("A001"), asset("A002"), asset("L001", domain="loan")])
    assert catalog.count() == 3
    assert catalog.count(business_domain="deposit") == 2
    assert catalog.require("A002").asset_name == "个人客户月日均存款"
    assert [item.asset_id for item in catalog.list(business_domain="loan")] == ["L001"]


def test_retrieval_then_three_layer_search_returns_duplicate(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [
            asset("A001"),
            asset("A002"),
            asset("L001", domain="loan"),
        ],
    )
    result = service.search_duplicates(
        SearchDuplicatesRequest(
            asset_id="A001",
            candidate_top_k=2,
            result_top_k=2,
            business_domain_only=False,
            include_not_duplicate=True,
        )
    )
    assert result.catalog_size == 3
    assert result.results[0].candidate.asset_id == "A002"
    assert result.results[0].comparison.decision == Decision.DUPLICATE
    assert result.results[0].comparison.fusion_score == pytest.approx(1.0)


def test_full_scan_deduplicates_symmetric_pairs_and_reduces_rerank(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [
            asset("A001"),
            asset("A002"),
            asset("L001", domain="loan"),
            asset("G001", domain="accounting"),
        ],
    )
    result = service.scan_all(
        FullScanRequest(
            candidate_top_k=1,
            business_domain_only=True,
        )
    )
    assert result.naive_pair_count == 6
    assert result.compared_pair_count == 1
    assert result.comparison_reduction_rate > 0.80
    assert result.duplicate_count == 1
    assert result.returned_pair_count == 1
    assert {result.pairs[0].asset_a_id, result.pairs[0].asset_b_id} == {
        "A001",
        "A002",
    }


def test_catalog_search_and_scan_api(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        [asset("A001"), asset("A002"), asset("L001", domain="loan")],
    )
    app = create_app(service)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["asset_catalog_size"] == 3

        listing = client.get("/api/v1/task1/assets?business_domain=deposit")
        assert listing.status_code == 200
        assert listing.json()["total"] == 2

        imported = client.post(
            "/api/v1/task1/assets",
            json=asset("A003").model_dump(mode="json"),
        )
        assert imported.status_code == 200
        assert imported.json()["catalog_size"] == 4
        stored = client.get("/api/v1/task1/assets/A003")
        assert stored.status_code == 200
        assert stored.json()["asset_id"] == "A003"

        search = client.post(
            "/api/v1/task1/search-duplicates",
            json={
                "asset_id": "A001",
                "candidate_top_k": 3,
                "result_top_k": 2,
                "business_domain_only": False,
                "include_not_duplicate": True,
            },
        )
        assert search.status_code == 200
        assert search.json()["results"][0]["candidate"]["asset_id"] == "A002"

        scan = client.post(
            "/api/v1/task1/scan-all",
            json={"candidate_top_k": 1, "business_domain_only": True},
        )
        assert scan.status_code == 200
        assert scan.json()["duplicate_count"] >= 1
