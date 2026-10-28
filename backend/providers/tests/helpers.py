from __future__ import annotations

from backend.providers.catalog import load_catalog
from backend.providers.settings import ProviderSettings
from backend.providers.testing.fake_provider_server import FakeProviderServer

KEYS = {"openai": "sk-test-openai-0000000000", "anthropic": "sk-ant-test-0000000000",
        "gemini": "AIzaTestGeminiKey000000", "xai": "xai-test-key-00000000000"}


def make_settings(server: FakeProviderServer | None = None, **over) -> ProviderSettings:
    base = {p: server.base for p in KEYS} if server else {}
    kw = dict(keys=dict(KEYS), base_urls=base, timeout_seconds=2.0, deadline_seconds=6.0, max_retries=2,
              backoff_base_seconds=0.01, backoff_cap_seconds=0.02, retry_after_cap_seconds=0.05,
              breaker_threshold=3, breaker_cooldown_seconds=0.3, allow_insecure_http=True,
              max_response_bytes=200_000)
    kw.update(over)
    return ProviderSettings(**kw)


CATALOG = load_catalog()
