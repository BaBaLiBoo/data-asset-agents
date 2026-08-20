"""总控模型客户端装配测试。"""

from typing import Any

from asset_supervisor import bootstrap
from asset_supervisor.config import Settings
from asset_supervisor.models.reflection import ResultReflection


class FakeChatModel:
    def __init__(self) -> None:
        self.schema: Any = None
        self.structured_options: dict[str, Any] = {}

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> "FakeChatModel":
        self.schema = schema
        self.structured_options = kwargs
        return self


def test_build_supervisor_uses_independent_non_thinking_reflection_model(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    models: list[FakeChatModel] = []

    def fake_init_chat_model(**kwargs: Any) -> FakeChatModel:
        calls.append(kwargs)
        model = FakeChatModel()
        models.append(model)
        return model

    monkeypatch.setattr(bootstrap, "init_chat_model", fake_init_chat_model)
    settings = Settings(
        model_name="qwen-requirement",
        reflection_model_name="qwen-reflection",
        reflection_enable_thinking=False,
        api_key="test-key",
        base_url="http://model.test/v1",
        asset_service_url="http://asset.test",
        sql_service_url="http://sql.test",
        lineage_service_url="http://lineage.test",
        professional_service_token=None,
        request_timeout_seconds=1,
        database_path=":memory:",
        database_url=None,
        memory_max_messages=12,
        memory_max_chars=6000,
    )

    supervisor = bootstrap.build_supervisor(settings)

    assert len(calls) == 2
    assert calls[0]["model"] == "qwen-requirement"
    assert "extra_body" not in calls[0]
    assert calls[1]["model"] == "qwen-reflection"
    assert calls[1]["extra_body"] == {"enable_thinking": False}
    assert supervisor._requirement_agent._model is models[0]
    assert supervisor._reflection_agent._structured_model is models[1]
    assert supervisor._lineage_client is not None
    assert supervisor._lineage_client._base_url == "http://lineage.test"
    assert models[1].schema is ResultReflection
    assert models[1].structured_options == {"method": "function_calling"}
