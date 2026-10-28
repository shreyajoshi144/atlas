from __future__ import annotations

import logging

import pytest

from backend.core.logging import JsonFormatter, redact_sensitive_text
from backend.core.retry import invoke_with_retry


@pytest.mark.parametrize("token", [
    "gsk_12345678901234567890123456789012",
    "sk-proj-12345678901234567890123456789012",
    "sk-12345678901234567890123456789012",
    "tvly-12345678901234567890123456789012",
])
def test_unlabelled_provider_tokens_are_redacted(token):
    output = redact_sensitive_text(f"provider rejected request with credential {token}")
    assert token not in output
    assert "REDACTED" in output


def test_json_formatter_redacts_provider_tokens_in_generic_error_field():
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "provider error", (), None)
    record.error = "request failed: gsk_12345678901234567890123456789012"
    output = JsonFormatter().format(record)
    assert "gsk_12345678901234567890123456789012" not in output
    assert "REDACTED" in output


class ProviderError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class ScriptedRunnable:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return {"answer": "ok", "messages": messages}


def test_retry_retries_429_but_not_payload_too_large():
    limited = ScriptedRunnable([ProviderError("rate limit", 429), None])
    assert invoke_with_retry(limited, "question", max_retries=1, base_delay_seconds=0)["answer"] == "ok"
    assert limited.calls == 2

    too_large = ScriptedRunnable([ProviderError("request entity too large", 413)])
    with pytest.raises(ProviderError):
        invoke_with_retry(too_large, "oversized prompt", max_retries=2, base_delay_seconds=0)
    assert too_large.calls == 1

    payload_error_with_rate_limit_wording = ScriptedRunnable([ProviderError("payload too large; rate limit bucket metadata", 413)])
    with pytest.raises(ProviderError):
        invoke_with_retry(payload_error_with_rate_limit_wording, "oversized prompt", max_retries=2, base_delay_seconds=0)
    assert payload_error_with_rate_limit_wording.calls == 1


def test_retry_exhaustion_is_bounded():
    limited = ScriptedRunnable([ProviderError("rate limit", 429) for _ in range(4)])
    with pytest.raises(ProviderError):
        invoke_with_retry(limited, "question", max_retries=2, base_delay_seconds=0)
    assert limited.calls == 3


def test_json_formatter_never_serializes_free_form_exception_or_content_fields():
    secret = "gsk_12345678901234567890123456789012"
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Operation failed", (), None)
    record.error = f"provider body included {secret} and private user prompt"
    record.prompt = "private user prompt"
    try:
        raise RuntimeError(f"database password=top-secret and payload={secret}")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()

    output = JsonFormatter().format(record)
    assert secret not in output
    assert "top-secret" not in output
    assert "private user prompt" not in output
    assert "database password" not in output
    assert '"exception_type": "RuntimeError"' in output
    assert '"stack_frames"' in output


def test_safe_server_error_returns_generic_detail_and_logs_only_safe_diagnostics(caplog):
    from backend.core.errors import safe_server_error

    logger = logging.getLogger("test.safe_server_error")
    secret = "tvly-12345678901234567890123456789012"
    try:
        raise RuntimeError(f"provider failed with {secret}; prompt=private research topic")
    except RuntimeError as exc:
        response = safe_server_error(logger, "Loading private report", exc)

    assert response.status_code == 500
    assert response.detail == "Loading private report failed. Please try again."
    assert secret not in caplog.text
    assert "private research topic" not in caplog.text
    assert any(getattr(record, "error_type", None) == "RuntimeError" for record in caplog.records)
    assert all(not hasattr(record, "error") for record in caplog.records if record.name == logger.name)
