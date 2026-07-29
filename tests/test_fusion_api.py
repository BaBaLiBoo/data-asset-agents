from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.fusion import ThreeLayerFusion
from app.keyword import KeywordComparator
from app.models import (
    AssetInput,
    ComparisonMode,
    ComparisonRequest,
    Decision,
    LayerScore,
)
from app.service import Task1Service
from tests.test_models import sample_asset


class FixedComparator:
    def __init__(self, score: LayerScore):
        self.score = score

    def compare(self, left, right):
        return self.score


class FixedSemantic(FixedComparator):
    class EmbeddingClient:
        model = "test-model"
        dimension = 384

    embedding_client = EmbeddingClient()


def layer(score: float, quality: float = 1.0) -> LayerScore:
    return LayerScore(score=score, quality=quality, available=True)


def test_quality_weighted_fusion_formula() -> None:
    outcome = ThreeLayerFusion().fuse(
        layer(0.90),
        layer(0.80),
        layer(0.70, quality=0.50),
    )
    assert outcome.score == pytest.approx((0.4 * 0.9 + 0.4 * 0.8 + 0.2 * 0.5 * 0.7) / 0.9)
    assert outcome.evidence_coverage == pytest.approx(0.9)
    assert outcome.decision == Decision.SUSPECTED_DUPLICATE


def test_high_score_with_low_coverage_is_not_auto_duplicate() -> None:
    outcome = ThreeLayerFusion().fuse(
        layer(0.99, 0.5),
        layer(0.99, 0.5),
        layer(0.99, 0.5),
    )
    assert outcome.score == pytest.approx(0.99)
    assert outcome.evidence_coverage == pytest.approx(0.5)
    assert outcome.decision == Decision.SUSPECTED_DUPLICATE
    assert outcome.warnings


def test_api_keeps_keyword_and_three_layer_outputs_separate() -> None:
    settings = replace(
        Settings(),
        embedding_api_key="test-only-not-used",
        embedding_model="test-model",
        embedding_dimension=384,
    )
    service = Task1Service(
        settings=settings,
        keyword=KeywordComparator(),
        semantic=FixedSemantic(layer(0.95)),
        logic=FixedComparator(layer(0.90)),
        lineage=FixedComparator(layer(0.85)),
        fusion=ThreeLayerFusion(),
        threshold_profiles={},
    )
    app = create_app(service)
    asset = AssetInput.model_validate(sample_asset())
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["embedding_model"] == "test-model"
        assert health.json()["embedding_dimension"] == 384

        keyword = client.post(
            "/api/v1/task1/compare",
            json=ComparisonRequest(
                mode=ComparisonMode.KEYWORD_BASELINE,
                asset_a=asset,
                asset_b=asset,
            ).model_dump(mode="json"),
        )
        assert keyword.status_code == 200
        assert keyword.json()["keyword_score"] == pytest.approx(1.0)
        assert keyword.json()["semantic"] is None

        three = client.post(
            "/api/v1/task1/compare",
            json=ComparisonRequest(
                mode=ComparisonMode.THREE_LAYER,
                asset_a=asset,
                asset_b=asset,
            ).model_dump(mode="json"),
        )
        assert three.status_code == 200
        assert three.json()["keyword_score"] is None
        assert three.json()["semantic"]["score"] == pytest.approx(0.95)
