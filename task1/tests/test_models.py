from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import AssetInput, ComparisonMode, ComparisonResult, Decision, LayerScore


def sample_asset() -> dict:
    return {
        "asset_id": "A001",
        "asset_name": "个人客户月日均存款",
        "description": "按客户和月份统计人民币存款平均余额",
        "business_domain": "deposit",
        "sql_text": "SELECT customer_id FROM dwd_account",
        "sql_dialect": "spark",
        "declared_grain": ["customer", "month"],
        "tables": [{"name": "dwd_account", "columns": [{"name": "customer_id"}]}],
        "lineage": {
            "direct_upstreams": ["dwd_account"],
            "root_sources": ["m_savings_account"],
            "coverage": 0.8,
            "freshness": 1.0,
            "source_reliability": 0.9,
        },
    }


def test_asset_contract_accepts_complete_asset() -> None:
    asset = AssetInput.model_validate(sample_asset())
    assert asset.asset_id == "A001"
    assert asset.declared_grain == ["customer", "month"]


def test_asset_contract_rejects_missing_grain() -> None:
    payload = sample_asset()
    payload["declared_grain"] = []
    with pytest.raises(ValidationError):
        AssetInput.model_validate(payload)


def test_result_contract_keeps_keyword_and_three_layer_isolated() -> None:
    keyword = ComparisonResult(
        mode=ComparisonMode.KEYWORD_BASELINE,
        decision=Decision.NOT_DUPLICATE,
        keyword_score=0.3,
    )
    assert keyword.semantic is None

    available = LayerScore(score=0.8, quality=1.0, available=True)
    three_layer = ComparisonResult(
        mode=ComparisonMode.THREE_LAYER,
        decision=Decision.SUSPECTED_DUPLICATE,
        semantic=available,
        logic=available,
        lineage=available,
        fusion_score=0.8,
        evidence_coverage=1.0,
    )
    assert three_layer.keyword_score is None


def test_settings_require_real_online_embedding_configuration(monkeypatch) -> None:
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    settings = Settings.from_env(env_path=Settings().project_root / "missing.env")
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        settings.require_embedding()


def test_settings_accept_configurable_embedding_model_and_dimension() -> None:
    settings = Settings(
        embedding_api_key="test-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="custom-model",
        embedding_dimension=384,
        embedding_send_dimensions=False,
    )

    settings.require_embedding()
