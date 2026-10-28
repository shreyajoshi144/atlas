"""Provider settings: keys and tuning come from the server environment only.

* Keys are read from process env, then from the project's ``.env`` (same file
  the rest of Atlas uses). They are never logged, never serialized and never
  returned by any API; ``__repr__`` redacts them.
* Placeholder values (``dummy``, ``test_key``, ``your-key-here`` ...) count as
  *not configured*, so a copied ``.env.example`` cannot make Atlas believe a
  provider is usable.
* Base URLs may be overridden (proxies/gateways) but must be https and, unless
  ``PROVIDER_ALLOW_CUSTOM_BASE_URL=1``, only the provider's own host.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PROVIDERS = ("openai", "anthropic", "gemini", "xai")

PROVIDER_LABELS = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini", "xai": "xAI Grok"}

KEY_ENV = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
}

DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "xai": "https://api.x.ai",
}

_ALLOWED_HOSTS = {
    "openai": {"api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "gemini": {"generativelanguage.googleapis.com"},
    "xai": {"api.x.ai", "us.api.x.ai", "eu.api.x.ai"},
}

_PLACEHOLDER = re.compile(r"(?i)^(dummy|test[_-]?key|changeme|change-me|placeholder|your[-_ ]?.*|xxx+|none|null|todo|replace.*|<.*>)$")


def _is_placeholder(value: str) -> bool:
    v = value.strip()
    return len(v) < 12 or bool(_PLACEHOLDER.match(v))


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip().removeprefix("export ").strip()] = v
    except OSError:
        pass
    return out


def _float(env: dict[str, str], name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(env.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def _int(env: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(env.get(name, default)))
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


@dataclass(frozen=True)
class ProviderSettings:
    keys: dict[str, str] = field(default_factory=dict, repr=False)
    base_urls: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 90.0            # per attempt
    deadline_seconds: float = 170.0          # across all attempts + backoff for one call
    max_retries: int = 2                     # retries after the first attempt
    backoff_base_seconds: float = 0.8
    backoff_cap_seconds: float = 8.0
    retry_after_cap_seconds: float = 20.0
    breaker_threshold: int = 5
    breaker_cooldown_seconds: float = 30.0
    per_provider_concurrency: int = 4
    global_concurrency: int = 12
    max_response_bytes: int = 4 * 1024 * 1024
    max_prompt_chars: int = 6000
    max_evidence_chars: int = 24000
    default_max_output_tokens: int = 4096
    max_output_tokens: int = 8192
    max_cost_per_request_usd: float = 0.50
    max_models_per_run: int = 4
    allow_insecure_http: bool = False        # tests / local fakes only
    discovery_ttl_seconds: float = 3600.0
    eval_dir: str = ""
    compare_retention_days: int = 90

    def key(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def configured(self, provider: str) -> bool:
        return bool(self.keys.get(provider))

    def base_url(self, provider: str) -> str:
        return self.base_urls.get(provider) or DEFAULT_BASE_URL[provider]

    def __repr__(self) -> str:  # never print keys
        configured = sorted(p for p in self.keys if self.keys[p])
        return f"ProviderSettings(configured={configured}, timeout={self.timeout_seconds}, retries={self.max_retries})"

    def secret_values(self) -> list[str]:
        return [v for v in self.keys.values() if v]


def load_settings(environ: dict[str, str] | None = None, dotenv_path: str | Path | None = None) -> ProviderSettings:
    """Build settings from env (+ .env). Pass ``environ`` in tests."""
    env: dict[str, str] = {}
    if environ is None:
        path = Path(dotenv_path) if dotenv_path else Path(__file__).resolve().parents[2] / ".env"
        env.update(_read_dotenv(path))
        env.update({k: v for k, v in os.environ.items()})   # process env wins over .env
    else:
        env.update(environ)

    keys: dict[str, str] = {}
    for provider, names in KEY_ENV.items():
        for name in names:
            val = (env.get(name) or "").strip()
            if val and not _is_placeholder(val):
                keys[provider] = val
                break

    allow_custom = env.get("PROVIDER_ALLOW_CUSTOM_BASE_URL") == "1"
    allow_http = env.get("PROVIDER_ALLOW_INSECURE_HTTP") == "1"
    base_urls: dict[str, str] = {}
    for provider in PROVIDERS:
        raw = (env.get(f"{provider.upper()}_BASE_URL") or "").strip().rstrip("/")
        if not raw:
            continue
        u = urlparse(raw)
        host_ok = (u.hostname or "") in _ALLOWED_HOSTS[provider] or allow_custom
        scheme_ok = u.scheme == "https" or (u.scheme == "http" and allow_http)
        if not (host_ok and scheme_ok and u.hostname):
            raise ValueError(
                f"{provider.upper()}_BASE_URL is not allowed: it must be https and the provider's own host "
                "(set PROVIDER_ALLOW_CUSTOM_BASE_URL=1 to permit a gateway).")
        base_urls[provider] = raw

    return ProviderSettings(
        keys=keys,
        base_urls=base_urls,
        timeout_seconds=_float(env, "PROVIDER_TIMEOUT_SECONDS", 90.0, 1.0, 600.0),
        deadline_seconds=_float(env, "PROVIDER_DEADLINE_SECONDS", 170.0, 1.0, 1200.0),
        max_retries=_int(env, "PROVIDER_MAX_RETRIES", 2, 0, 5),
        backoff_base_seconds=_float(env, "PROVIDER_BACKOFF_BASE_SECONDS", 0.8, 0.0, 30.0),
        backoff_cap_seconds=_float(env, "PROVIDER_BACKOFF_CAP_SECONDS", 8.0, 0.0, 120.0),
        retry_after_cap_seconds=_float(env, "PROVIDER_RETRY_AFTER_CAP_SECONDS", 20.0, 0.0, 120.0),
        breaker_threshold=_int(env, "PROVIDER_BREAKER_THRESHOLD", 5, 1, 100),
        breaker_cooldown_seconds=_float(env, "PROVIDER_BREAKER_COOLDOWN_SECONDS", 30.0, 0.0, 3600.0),
        per_provider_concurrency=_int(env, "PROVIDER_CONCURRENCY", 4, 1, 64),
        global_concurrency=_int(env, "PROVIDER_GLOBAL_CONCURRENCY", 12, 1, 256),
        max_prompt_chars=_int(env, "COMPARE_MAX_PROMPT_CHARS", 6000, 100, 50000),
        max_evidence_chars=_int(env, "COMPARE_MAX_EVIDENCE_CHARS", 24000, 0, 200000),
        default_max_output_tokens=_int(env, "COMPARE_DEFAULT_MAX_OUTPUT_TOKENS", 4096, 64, 32768),
        max_output_tokens=_int(env, "COMPARE_MAX_OUTPUT_TOKENS", 8192, 64, 65536),
        max_cost_per_request_usd=_float(env, "COMPARE_MAX_COST_PER_REQUEST_USD", 0.50, 0.0, 1000.0),
        max_models_per_run=_int(env, "COMPARE_MAX_MODELS", 4, 1, 4),
        allow_insecure_http=allow_http,
        discovery_ttl_seconds=_float(env, "PROVIDER_DISCOVERY_TTL_SECONDS", 3600.0, 0.0, 86400.0),
        eval_dir=(env.get("COMPARE_EVAL_DIR") or "").strip(),
        compare_retention_days=_int(env, "COMPARE_RETENTION_DAYS", 90, 1, 3650),
    )
