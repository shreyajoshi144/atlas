from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Atlas AI"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Groq (primary LLM — free)
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    openai_model: str = "llama-3.3-70b-versatile"
    openai_temperature: float = 0.1
    openai_max_tokens: int = 4000

    # OpenAI (only needed for embeddings — optional)
    openai_api_key: str = Field(default="dummy", alias="OPENAI_API_KEY")

    tavily_api_key: str = Field(..., alias="TAVILY_API_KEY")
    tavily_max_results: int = 10

    sqlite_path: str = str(DATA_DIR / "sqlite" / "atlas.db")
    chroma_path: str = str(DATA_DIR / "chroma")

    request_timeout_seconds: int = 20
    scrape_timeout_seconds: int = 12
    scrape_max_retries: int = 2
    scrape_concurrency: int = 5

    default_top_k_urls: int = 5
    max_content_chars_per_source: int = 6000

    analytics_enabled: bool = True
    estimated_input_cost_per_1k_tokens: float = 0.0
    estimated_output_cost_per_1k_tokens: float = 0.0

    user_agent: str = (
        "AtlasAI/1.0 "
        "(Research Intelligence Platform; +https://example.local)"
    )

    sentry_dsn: Optional[str] = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sqlite").mkdir(parents=True, exist_ok=True)
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
