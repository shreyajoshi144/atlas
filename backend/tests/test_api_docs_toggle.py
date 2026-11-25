"""API docs are public documentation: off in production unless ENABLE_API_DOCS=true."""
from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from backend.core.config import get_settings


def _load_main(monkeypatch, env: str, enable: str | None):
    monkeypatch.setenv("APP_ENV", env)
    if enable is None:
        monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    else:
        monkeypatch.setenv("ENABLE_API_DOCS", enable)
    if env == "production":
        monkeypatch.setenv("SECRET_KEY", "Qm7vR2xT9pLk4Zb8WnYc1HfJd6GsAe3UoIyXtB5NvCr0MzKq")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "5")
    get_settings.cache_clear()
    sys.modules.pop("main", None)
    return importlib.import_module("main")


@pytest.fixture(autouse=True)
def _restore():
    yield
    sys.modules.pop("main", None)
    get_settings.cache_clear()


def test_docs_on_in_development(monkeypatch):
    m = _load_main(monkeypatch, "development", None)
    assert m.app.docs_url == "/docs" and m.app.openapi_url == "/openapi.json"


def test_docs_off_in_production(monkeypatch):
    m = _load_main(monkeypatch, "production", None)
    assert m.app.docs_url is None and m.app.redoc_url is None and m.app.openapi_url is None
    c = TestClient(m.app)
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_docs_can_be_enabled_in_production(monkeypatch):
    m = _load_main(monkeypatch, "production", "true")
    assert m.app.docs_url == "/docs"
