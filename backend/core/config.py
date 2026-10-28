from functools import lru_cache
import math
import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
# Runtime paths may be redirected by deployments and test harnesses. Keeping
# test data outside the source tree prevents imports from modifying the repo.
DATA_DIR = Path(os.environ.get("ATLAS_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
LOG_DIR = Path(os.environ.get("ATLAS_LOG_DIR", str(DATA_DIR / "logs"))).expanduser()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    app_name: str = "Atlas AI"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Groq (primary LLM — free)
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    openai_model: str = "openai/gpt-oss-120b"
    openai_temperature: float = 0.1
    # Root cause of a real production failure (2026-09-26 live run, investigate
    # mode, 20 sources, 24000 evidence chars sent): at 1600 this was too small
    # for the LLM to finish either the VerificationOutput JSON (up to 5 claims
    # x 2 evidence items) or the WriterOutput JSON (a full multi-section
    # markdown report). The response was cut off mid-string (finish_reason
    # "length"), which _coerce_json_object correctly refused to guess-repair,
    # surfacing as "malformed JSON" / "Unterminated string" — the parser was
    # never the bug, the output budget was. Raised so deeper research modes
    # can actually complete their structured output.
    openai_max_tokens: int = 8192
    # Writer (final report) call budget. Live run 2026-10-25 (literature_review,
    # 20 sources, ~5.4k prompt tokens): the unbounded "detailed" report used all
    # 8192 completion tokens (finish_reason "length"), then the recovery call
    # resent the same prompt with the same budget and Groq rejected it with
    # HTTP 413 (request too large). The writer now asks for a length-bounded
    # report and its single recovery call requests a strictly smaller output.
    writer_max_output_tokens: int = Field(default=8192, ge=256, alias="WRITER_MAX_OUTPUT_TOKENS")
    writer_recovery_max_output_tokens: int = Field(default=4096, ge=256, alias="WRITER_RECOVERY_MAX_OUTPUT_TOKENS")
    # Per-request HTTP timeout for writer calls only. Generating a full report
    # took ~18s of the global 20s request_timeout_seconds in the same run.
    writer_request_timeout_seconds: int = Field(default=60, ge=1, alias="WRITER_REQUEST_TIMEOUT_SECONDS")

    # OpenAI (only needed for embeddings — optional)
    openai_api_key: str = Field(default="dummy", alias="OPENAI_API_KEY")

    tavily_api_key: str = Field(..., alias="TAVILY_API_KEY")
    tavily_max_results: int = 20

    sqlite_path: str = str(DATA_DIR / "sqlite" / "atlas.db")
    chroma_path: str = str(DATA_DIR / "chroma")

    request_timeout_seconds: int = 20
    scrape_timeout_seconds: int = 12
    scrape_max_retries: int = 2
    scrape_concurrency: int = 5

    default_top_k_urls: int = 4

    # ── M5 retrieval (additive; defaults reproduce sensible hybrid behaviour) ──
    # Dense embedding model used for source ranking and report-memory search.
    embedding_model_name: str = Field(default="all-MiniLM-L6-v2", alias="EMBEDDING_MODEL_NAME")
    # Reciprocal-rank-fusion constant and per-retriever weights.
    retrieval_rrf_k: int = Field(default=60, ge=1, alias="RETRIEVAL_RRF_K")
    retrieval_lexical_weight: float = Field(default=1.0, ge=0.0, alias="RETRIEVAL_LEXICAL_WEIGHT")
    retrieval_dense_weight: float = Field(default=1.0, ge=0.0, alias="RETRIEVAL_DENSE_WEIGHT")
    # How many candidates each first-stage retriever may contribute to fusion.
    retrieval_candidate_depth: int = Field(default=50, ge=1, alias="RETRIEVAL_CANDIDATE_DEPTH")
    # Upper bound on one user's chunks scanned per memory query (guards latency).
    retrieval_max_scan_chunks: int = Field(default=5000, ge=1, alias="RETRIEVAL_MAX_SCAN_CHUNKS")
    # A failed embedding backend is retried no more often than this.
    embedding_retry_seconds: int = Field(default=300, ge=0, alias="EMBEDDING_RETRY_SECONDS")
    max_content_chars_per_source: int = 2200

    # Hard ceiling on how much evidence text any single LLM call ever sees.
    # Observed live (2026-09-26, org on_demand tier): openai/gpt-oss-120b is
    # rate-limited to 8,000 tokens/minute (Groq 429 response: "Limit 8000"),
    # not the ~12,000 previously assumed here. This pipeline fires 2-3 LLM
    # calls back to back for one research run, so at deeper research modes a
    # single call's input+output can legitimately exceed the per-minute quota
    # on its own — invoke_with_retry (core/llm.py) already waits out the 429
    # and retries, which is the correct behavior here (added latency, not a
    # failure), rather than something this constant alone can fully prevent.
    max_evidence_chars_per_llm_call: int = 24000

    analytics_enabled: bool = True
    estimated_input_cost_per_1k_tokens: float = 0.0
    estimated_output_cost_per_1k_tokens: float = 0.0

    user_agent: str = (
        "AtlasAI/1.0 "
        "(Research Intelligence Platform; +https://example.local)"
    )

    sentry_dsn: Optional[str] = None

    # Auth
    secret_key: str = Field(default="dev-insecure-secret-change-me-in-.env", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(default=60 * 24 * 7, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    password_reset_expire_minutes: int = Field(default=30, alias="PASSWORD_RESET_EXPIRE_MINUTES")
    # Used only to build the link logged/emailed for password reset — no
    # email provider is configured, so this just needs to point at wherever
    # the frontend is actually served from.
    frontend_base_url: str = Field(default="http://localhost:5500", alias="FRONTEND_BASE_URL")

    # CORS — comma-separated explicit origin list. Only enforced in
    # production (see validate_production_config / main.py); development
    # keeps allowing "*" so nothing extra is required to run this locally.
    cors_allowed_origins: str = Field(
        default="http://localhost:5500,http://127.0.0.1:5500",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    # Request-body size ceiling, checked at the middleware level before any
    # route/pydantic validation runs — a coarse first line of defense
    # against obviously-oversized payloads regardless of per-field limits.
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, alias="MAX_REQUEST_BODY_BYTES")

    # Central model-call guardrails. Per-user admin overrides live in SQLite;
    # these defaults apply to every account without an override.
    default_daily_llm_call_limit: int = Field(default=1000, ge=0, alias="DEFAULT_DAILY_LLM_CALL_LIMIT")
    default_monthly_llm_spend_cap_usd: float | None = Field(default=None, ge=0.0, alias="DEFAULT_MONTHLY_LLM_SPEND_CAP_USD")

    # Optional abstention threshold for AtlasBench output arbitration. This is
    # a measured composite-score floor, not a confidence probability. Zero
    # preserves the historical selection policy unless a deployment opts in.
    atlasbench_min_arbitration_quality: float = Field(default=0.0, ge=0.0, le=1.0, alias="ATLASBENCH_MIN_ARBITRATION_QUALITY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    # Importing the application must not write beside the code merely to load
    # configuration. Create only configured runtime directories.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return settings


class ConfigurationError(RuntimeError):
    """Raised when required security configuration is missing or unsafe
    for the environment the app is running in."""


# Known-insecure SECRET_KEY values worth blocking by name, on top of the
# length check below — catches the common "left it as the example value"
# and "typed something short as a placeholder" mistakes specifically.
_INSECURE_SECRET_KEYS = {
    "",
    "dev-insecure-secret-change-me-in-.env",
    "changeme",
    "change-me",
    "secret",
    "insecure",
    "test",
    "test_key",
    "password",
    "replace-with-a-random-secret-of-at-least-32-characters",
    "replace-with-your-secret-key",
    "your-secret-key",
    "example-secret-key",
    "placeholder-secret-key",
    "dummy-secret-key",
}
_MIN_SECRET_KEY_LENGTH = 32
_MIN_SECRET_KEY_ENTROPY_BITS_PER_CHAR = 3.0
_PREDICTABLE_SEQUENCES = (
    "0123456789", "1234567890", "9876543210", "abcdefghijklmnopqrstuvwxyz",
    "zyxwvutsrqponmlkjihgfedcba", "qwertyuiop", "asdfghjkl", "zxcvbnm",
)
_PREDICTABLE_PREFIXES = ("password", "letmein", "welcome", "admin", "secret", "qwerty", "iloveyou", "default")


def _is_insecure_secret_key(value: str) -> bool:
    normalized = value.strip().lower()
    if (normalized in _INSECURE_SECRET_KEYS
            or any(marker in normalized for marker in ("replace-with-", "your-secret", "example-secret", "placeholder", "dummy-secret"))
            or normalized.startswith(("dev-insecure-", "test-secret-"))):
        return True
    if (len(set(normalized)) <= 1
            or any(sequence in normalized for sequence in _PREDICTABLE_SEQUENCES)
            or normalized.startswith(_PREDICTABLE_PREFIXES)):
        return True
    # Repeated short patterns (e.g. abc123 repeated) look long but carry little
    # entropy. Bound period detection to common human-typed patterns.
    for period in range(1, min(8, len(normalized) // 2) + 1):
        if all(char == normalized[index % period] for index, char in enumerate(normalized)):
            return True
    counts: dict[str, int] = {}
    for char in normalized:
        counts[char] = counts.get(char, 0) + 1
    entropy = -sum((count / len(normalized)) * math.log2(count / len(normalized)) for count in counts.values())
    return entropy < _MIN_SECRET_KEY_ENTROPY_BITS_PER_CHAR


def validate_production_config(settings: Settings) -> None:
    """Fail loudly rather than silently signing every auth token with a
    secret that's default, guessable, or too short to resist brute force.
    Only enforced when app_env is "production" — local/dev/staging runs
    are unaffected, so nothing extra is required to run this locally.
    Call this once at startup (see main.py's lifespan)."""
    if settings.app_env != "production":
        return

    key = settings.secret_key
    if not key or _is_insecure_secret_key(key) or len(key) < _MIN_SECRET_KEY_LENGTH:
        raise ConfigurationError(
            "SECRET_KEY is missing, predictable/low-entropy, a known-insecure default, or shorter than "
            f"{_MIN_SECRET_KEY_LENGTH} characters. Set a real, random SECRET_KEY "
            "in the production environment before starting the app — leaving "
            "it unset or weak lets anyone forge valid login tokens. Generate "
            "one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    origins = settings.cors_allowed_origins_list
    if not origins or "*" in origins:
        raise ConfigurationError(
            "CORS_ALLOWED_ORIGINS must be set to an explicit, comma-separated "
            "list of allowed frontend origins in production — wildcard ('*') "
            "or an empty list would let any website make authenticated "
            "requests against this API from a user's browser. Example: "
            "CORS_ALLOWED_ORIGINS=https://your-frontend.example.com"
        )

    if settings.default_monthly_llm_spend_cap_usd is None:
        raise ConfigurationError(
            "DEFAULT_MONTHLY_LLM_SPEND_CAP_USD must be explicitly configured "
            "in production so account spend limits are enforced."
        )
