from __future__ import annotations

import asyncio
import json

import pytest


def test_production_config_requires_real_secret_and_explicit_cors(monkeypatch):
    from backend.core.config import ConfigurationError, Settings, validate_production_config
    import secrets

    with pytest.raises(ConfigurationError):
        validate_production_config(Settings(app_env="production", secret_key="short"))

    validate_production_config(
        Settings(
            app_env="production",
            secret_key=secrets.token_urlsafe(48),
            cors_allowed_origins="https://atlas.example.com",
            default_monthly_llm_spend_cap_usd=25.0,
        )
    )


def test_log_formatter_redacts_secret_bearer_and_api_key_values():
    import logging
    from backend.core.logging import JsonFormatter

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed Bearer abc.def.ghi api_key=super-secret",
        args=(),
        exc_info=None,
    )
    record.authorization = "Bearer another-secret"
    payload = json.loads(JsonFormatter().format(record))
    rendered = json.dumps(payload)

    assert "abc.def.ghi" not in rendered
    assert "super-secret" not in rendered
    assert "another-secret" not in rendered
    assert "***REDACTED***" in rendered


def test_research_run_guard_blocks_same_user_and_releases():
    from backend.auth.rate_limit import (
        acquire_research_run,
        release_research_run,
        reset_research_run_guards,
    )
    from fastapi import HTTPException

    reset_research_run_guards()
    acquire_research_run("user_a")
    with pytest.raises(HTTPException) as exc:
        acquire_research_run("user_a")
    assert exc.value.status_code == 409

    acquire_research_run("user_b")
    release_research_run("user_a")
    acquire_research_run("user_a")
    release_research_run("user_a")
    release_research_run("user_b")
    reset_research_run_guards()


def test_llm_and_search_timeout_configuration_is_wired():
    from backend.core.config import Settings

    settings = Settings(
        groq_api_key="test",
        tavily_api_key="test",
        request_timeout_seconds=17,
    )
    assert settings.request_timeout_seconds == 17

    # Static contract: the configured timeout is passed to both provider
    # clients rather than being an unused setting.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    llm_source = (root / "core" / "llm.py").read_text()
    tavily_source = (root / "services" / "tavily_service.py").read_text()
    assert "timeout=settings.request_timeout_seconds" in llm_source
    assert 'search_kwargs["timeout"]' in tavily_source


def test_finalization_uses_persisted_report_id():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "graph" / "nodes.py").read_text()
    assert 'metadata={"report_id": state["final_report"].report_id}' in source


def test_safe_server_error_hides_details_from_response_and_only_logs_them_in_development(monkeypatch):
    import logging

    from backend.core import errors
    from backend.core.config import Settings

    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("test.safe_server_error")
    log.addHandler(H())
    try:
        raise ValueError("secret-detail-123")
    except ValueError as exc:
        for env, debug, expect_detail in (("development", True, True), ("production", True, False), ("development", False, False)):
            monkeypatch.setattr("backend.core.config.get_settings", lambda e=env, d=debug: Settings(app_env=e, debug=d))
            records.clear()
            http = errors.safe_server_error(log, "Research run", exc)
            assert "secret-detail-123" not in http.detail and http.status_code == 500
            assert hasattr(records[0], "debug_error_message") is expect_detail
            assert hasattr(records[0], "debug_traceback") is expect_detail
