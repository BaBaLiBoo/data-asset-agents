from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://minibank:minibank@localhost:5432/minibank"
    database_connect_timeout: int = Field(default=5, ge=1, le=60)
    sql_statement_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    sql_max_rows: int = Field(default=200, ge=1, le=1000)

    llm_mode: Literal["mock", "live"] = "mock"
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: int = Field(default=30, ge=1, le=120)

    embedding_provider: str = "aliyun"
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dimensions: int = Field(default=1024, ge=8, le=4096)

    ontology_path: Path = Path("ontology/retail_banking")
    api_base_url: str = "http://localhost:8000"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

