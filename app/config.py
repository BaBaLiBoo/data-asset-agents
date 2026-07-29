from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    env_path: Path = field(default_factory=lambda: PROJECT_ROOT / ".env")
    embedding_api_key: str | None = None
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 0
    embedding_send_dimensions: bool = True
    embedding_api_key_header: str = "Authorization"
    embedding_api_key_prefix: str = "Bearer"
    embedding_batch_size: int = 10
    embedding_timeout_seconds: float = 30.0
    embedding_max_retries: int = 2
    embedding_cache_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "embedding_cache.db"
    )
    calibrated_thresholds_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "calibrated_thresholds.json"
    )
    retrieval_thresholds_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "retrieval_thresholds.json"
    )
    asset_catalog_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "asset_catalog.db"
    )
    bootstrap_assets_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "evaluation_v3" / "assets.jsonl"
    )
    auto_bootstrap_catalog: bool = True
    retrieval_vector_weight: float = 0.60
    retrieval_keyword_weight: float = 0.20
    retrieval_table_weight: float = 0.10
    retrieval_field_weight: float = 0.10
    retrieval_candidate_top_k: int = 20
    retrieval_result_top_k: int = 10
    text_search_vector_weight: float = 0.80
    text_search_keyword_weight: float = 0.20
    text_search_default_top_k: int = 5
    text_search_max_top_k: int = 20
    text_search_min_score: float = 0.60
    sql_search_embedding_weight: float = 0.50
    sql_search_keyword_weight: float = 0.10
    sql_search_logic_weight: float = 0.30
    sql_search_identifier_weight: float = 0.10
    sql_search_min_score: float = 0.60
    semantic_weight: float = 0.40
    logic_weight: float = 0.40
    lineage_weight: float = 0.20
    suspected_threshold: float = 0.75
    duplicate_threshold: float = 0.88
    minimum_evidence_coverage: float = 0.70
    algorithm_version: str = "task1_v1-1.1"

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Settings":
        path = env_path or PROJECT_ROOT / ".env"
        if path.exists():
            load_dotenv(path, override=False)

        send_dimensions_value = os.getenv("EMBEDDING_SEND_DIMENSIONS", "true")
        send_dimensions = send_dimensions_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            env_path=path,
            embedding_api_key=(
                os.getenv("EMBEDDING_API_KEY")
                or os.getenv("DASHSCOPE_API_KEY")
            ),
            embedding_base_url=(
                os.getenv("EMBEDDING_BASE_URL")
                or os.getenv("DASHSCOPE_BASE_URL")
                or ""
            ).rstrip("/"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "0")),
            embedding_send_dimensions=send_dimensions,
            embedding_api_key_header=os.getenv(
                "EMBEDDING_API_KEY_HEADER",
                "Authorization",
            ).strip(),
            embedding_api_key_prefix=os.getenv(
                "EMBEDDING_API_KEY_PREFIX",
                "Bearer",
            ).strip(),
            embedding_batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "10")),
            embedding_timeout_seconds=float(
                os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30")
            ),
            embedding_max_retries=int(os.getenv("EMBEDDING_MAX_RETRIES", "2")),
            text_search_vector_weight=float(
                os.getenv("TEXT_SEARCH_VECTOR_WEIGHT", "0.80")
            ),
            text_search_keyword_weight=float(
                os.getenv("TEXT_SEARCH_KEYWORD_WEIGHT", "0.20")
            ),
            text_search_default_top_k=int(
                os.getenv("TEXT_SEARCH_DEFAULT_TOP_K", "5")
            ),
            text_search_max_top_k=int(
                os.getenv("TEXT_SEARCH_MAX_TOP_K", "20")
            ),
            text_search_min_score=float(
                os.getenv("TEXT_SEARCH_MIN_SCORE", "0.60")
            ),
            sql_search_embedding_weight=float(
                os.getenv("SQL_SEARCH_EMBEDDING_WEIGHT", "0.50")
            ),
            sql_search_keyword_weight=float(
                os.getenv("SQL_SEARCH_KEYWORD_WEIGHT", "0.10")
            ),
            sql_search_logic_weight=float(
                os.getenv("SQL_SEARCH_LOGIC_WEIGHT", "0.30")
            ),
            sql_search_identifier_weight=float(
                os.getenv("SQL_SEARCH_IDENTIFIER_WEIGHT", "0.10")
            ),
            sql_search_min_score=float(
                os.getenv("SQL_SEARCH_MIN_SCORE", "0.60")
            ),
        )

    def require_embedding(self) -> None:
        if not self.embedding_api_key:
            raise RuntimeError(
                "EMBEDDING_API_KEY is required; set it in the local .env file "
                "or process environment"
            )
        if not self.embedding_base_url.startswith(("http://", "https://")):
            raise RuntimeError(
                "EMBEDDING_BASE_URL must be an http:// or https:// URL"
            )
        if not self.embedding_model:
            raise RuntimeError("EMBEDDING_MODEL is required")
        if self.embedding_dimension <= 0:
            raise RuntimeError("EMBEDDING_DIMENSION must be a positive integer")
        if not self.embedding_api_key_header:
            raise RuntimeError("EMBEDDING_API_KEY_HEADER cannot be empty")
        if self.embedding_batch_size <= 0:
            raise RuntimeError("EMBEDDING_BATCH_SIZE must be a positive integer")
        if self.embedding_timeout_seconds <= 0:
            raise RuntimeError(
                "EMBEDDING_TIMEOUT_SECONDS must be a positive number"
            )
        if self.embedding_max_retries < 0:
            raise RuntimeError("EMBEDDING_MAX_RETRIES cannot be negative")

    def ensure_directories(self) -> None:
        self.embedding_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_catalog_path.parent.mkdir(parents=True, exist_ok=True)
        (self.project_root / "reports").mkdir(parents=True, exist_ok=True)


settings = Settings.from_env()
