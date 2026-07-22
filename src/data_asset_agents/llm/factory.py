from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError


class ModelFactory:
    """Build OpenAI-compatible clients without coupling business logic to a vendor."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chat_model(self) -> ChatOpenAI:
        key = self.settings.llm_api_key.get_secret_value()
        if self.settings.llm_mode == "mock" or not key:
            raise DataAssetAgentsError(
                "Live chat model is disabled; deterministic mock mode is active"
            )
        return ChatOpenAI(
            model=self.settings.llm_model,
            base_url=self.settings.llm_base_url,
            api_key=key,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
            temperature=0,
            max_tokens=self.settings.llm_max_output_tokens,
        )

    def embeddings(self) -> OpenAIEmbeddings:
        key = self.settings.embedding_api_key.get_secret_value()
        if not key:
            raise DataAssetAgentsError("EMBEDDING_API_KEY is required for live vector retrieval")
        return OpenAIEmbeddings(
            model=self.settings.embedding_model,
            base_url=self.settings.embedding_base_url,
            api_key=key,
            dimensions=self.settings.embedding_dimensions,
            check_embedding_ctx_length=False,
            request_timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
        )
