"""总控、模型和专业服务环境配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    model_name: str
    reflection_model_name: str
    reflection_enable_thinking: bool
    api_key: str | None
    base_url: str | None
    asset_service_url: str
    sql_service_url: str
    lineage_service_url: str
    professional_service_token: str | None
    request_timeout_seconds: float
    database_path: str
    database_url: str | None
    memory_max_messages: int
    memory_max_chars: int

    @property
    def model_enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls) -> "Settings":
        demo_root = Path(__file__).resolve().parents[1]
        load_dotenv(demo_root / ".env", override=False)
        model_name = os.getenv("DASHSCOPE_MODEL", "qwen3.7-flash")

        return cls(
            model_name=model_name,
            reflection_model_name=os.getenv("REFLECTION_MODEL") or model_name,
            reflection_enable_thinking=_environment_bool(
                "REFLECTION_ENABLE_THINKING",
                False,
            ),
            api_key=os.getenv("DASHSCOPE_API_KEY") or None,
            base_url=os.getenv("DASHSCOPE_BASE_URL") or None,
            asset_service_url=os.getenv(
                "ASSET_SERVICE_URL",
                "http://127.0.0.1:8001",
            ),
            sql_service_url=os.getenv(
                "SQL_SERVICE_URL",
                "http://127.0.0.1:8002",
            ),
            lineage_service_url=os.getenv(
                "LINEAGE_SERVICE_URL",
                "http://127.0.0.1:8003",
            ),
            professional_service_token=(
                os.getenv("PROFESSIONAL_SERVICE_TOKEN") or None
            ),
            request_timeout_seconds=float(
                os.getenv("REQUEST_TIMEOUT_SECONDS", "60")
            ),
            database_path=os.getenv(
                "TASK_DATABASE_PATH",
                str(demo_root / "data" / "asset_agent.db"),
            ),
            database_url=os.getenv("DATABASE_URL") or None,
            memory_max_messages=int(os.getenv("MEMORY_MAX_MESSAGES", "12")),
            memory_max_chars=int(os.getenv("MEMORY_MAX_CHARS", "6000")),
        )
