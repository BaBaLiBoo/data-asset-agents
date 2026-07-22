from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory


def test_live_embeddings_preserve_string_inputs_for_compatible_providers() -> None:
    settings = Settings(
        llm_mode="live",
        embedding_api_key="test-only",
        embedding_dimensions=32,
    )

    client = ModelFactory(settings).embeddings()

    assert client.check_embedding_ctx_length is False
