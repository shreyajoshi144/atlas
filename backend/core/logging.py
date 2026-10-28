import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any
from backend.core.config import LOG_DIR, get_settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="-")

# Defense-in-depth: any `extra={...}` value whose key matches one of these
# (case-insensitive, substring match) is redacted before it ever reaches a
# log line — regardless of which call site put it there. This is a
# safety net, not a substitute for not logging secrets in the first place.
_REDACTED_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
)
_REDACTED_VALUE = "***REDACTED***"
# Free-form diagnostic fields can contain user prompts, documents, provider
# payloads, internal URLs, or exception messages. Keep the field for schema
# stability but never serialize its raw value. Use error_type/status_code and
# sanitized stack frames for diagnosis instead.
_SENSITIVE_VALUE_KEYS = {
    "error", "exception", "traceback", "tb", "prompt", "completion",
    "content", "query", "topic", "messages", "document", "document_text",
    "raw_response", "response_body",
}

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer)\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+"),
    # Common provider credentials can appear in exception bodies without a key
    # label. Redact recognizable token prefixes even inside a generic `error` field.
    re.compile(r"(?i)\b(gsk_[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,})\b"),
)

def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.pattern.startswith(r"(?i)\b(gsk_"):
            redacted = pattern.sub("***REDACTED***", redacted)
        else:
            redacted = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", redacted)
    return redacted


def safe_traceback_frames(tb: TracebackType | None, *, limit: int = 12) -> list[dict[str, Any]]:
    """Return stack locations without exception text, locals, or absolute paths."""
    if tb is None:
        return []
    frames = traceback.extract_tb(tb, limit=limit)
    return [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in frames
    ]


def _redact_extra(extra_keys: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in extra_keys.items():
        lowered = key.lower()
        if lowered in _SENSITIVE_VALUE_KEYS or any(marker in lowered for marker in _REDACTED_KEY_MARKERS):
            redacted[key] = _REDACTED_VALUE
        else:
            redacted[key] = redact_sensitive_text(value) if isinstance(value, str) else value
    return redacted


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "request_id": request_id_ctx.get(),
            "session_id": session_id_ctx.get(),
        }

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            # Do not format the exception message: provider errors often embed
            # credentials, request bodies, document text, or internal URLs.
            payload["exception_type"] = exc_type.__name__ if exc_type else "Exception"
            payload["stack_frames"] = safe_traceback_frames(exc_tb)

        extra_keys = {
            key: value
            for key, value in record.__dict__.items()
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message"
            }
        }
        payload.update(_redact_extra(extra_keys))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    formatter = JsonFormatter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    file_path = Path(LOG_DIR) / "datasense.log"
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

def set_request_context(request_id: str, session_id: str | None = None) -> None:
    request_id_ctx.set(request_id)
    if session_id:
        session_id_ctx.set(session_id)