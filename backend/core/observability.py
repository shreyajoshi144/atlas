"""Safe, structured observability for research runs.

Trace payloads are deliberately metadata-only. External content, prompts,
credentials and raw tool inputs are never accepted by the trace helpers.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

_SECRET_KEY_MARKERS = (
    "password", "secret", "token", "api_key", "apikey", "authorization",
    "access_token", "refresh_token", "cookie", "set-cookie",
)
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gsk_[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
)


def classify_error(error: Any) -> str:
    text = str(error or "").lower()
    name = type(error).__name__.lower() if error is not None else ""
    if any(x in text or x in name for x in ("permission", "forbidden", "unauthorized", "authentication", "authorization")):
        return "authentication/authorization"
    if any(x in text or x in name for x in ("timeout", "timed out")):
        return "timeout"
    if any(x in text or x in name for x in ("http", "connection", "dns", "network", "socket")):
        return "network"
    if any(x in text or x in name for x in ("tavily", "openai", "groq", "provider", "api")):
        return "provider/API"
    if any(x in text or x in name for x in ("scrap", "ssrf", "url")):
        return "scraping"
    if any(x in text or x in name for x in ("parse", "json", "validation")):
        return "parsing"
    if "verif" in text or "verif" in name:
        return "verification"
    if any(x in text or x in name for x in ("sqlite", "database", "sql", "foreign key")):
        return "database"
    if "invalid" in text or "valueerror" in name or "validationerror" in name:
        return "validation"
    return "internal"


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 2:
        return "[truncated]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            text = value[:300]
            for pattern in _SENSITIVE_TEXT_PATTERNS:
                text = pattern.sub("[REDACTED]", text)
            return text
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in _SECRET_KEY_MARKERS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:20]]
    return str(value)[:300]


def append_trace(
    state: Mapping[str, Any],
    *,
    component: str,
    action: str,
    status: str,
    duration_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    error: Any = None,
    event_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return a new safe trace list; never stores raw inputs/content."""
    trace = list(state.get("research_trace", []))
    previous = trace[-1].get("event_id") if trace else None
    session = state.get("session")
    run_id = getattr(session, "session_id", None) or state.get("session_id") or "unknown"
    user_id = state.get("authenticated_user_id")
    event: dict[str, Any] = {
        "run_id": str(run_id),
        "event_id": event_id or f"trace_{uuid.uuid4().hex}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "action": action,
        "status": status,
        "duration_ms": max(0, int(duration_ms or 0)),
        "parent_event_id": previous,
        "metadata": _safe_value(dict(metadata or {})),
    }
    if user_id:
        event["user_id"] = str(user_id)  # internal persistence only; API strips it.
    if error is not None:
        event["error_type"] = classify_error(error)
        event["error_message"] = _safe_value(str(error))
    trace.append(event)
    return trace



_PUBLIC_AGENTIC_BLOCKED_KEYS = {
    "user_id", "run_id", "user_identifier", "authenticated_user_id",
    "claim", "claim_text", "missing_evidence", "evidence", "evidence_text",
    "content", "raw_content", "tool_input", "tool_inputs", "payload",
    "prompt", "completion", "secret", "api_key", "authorization",
}

def serialize_public_agentic_trace(trace: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Serialize the rich internal agent trace into a metadata-only public trace."""
    def sanitize(value: Any, key: str | None = None, depth: int = 0) -> Any:
        if key and key.lower() in _PUBLIC_AGENTIC_BLOCKED_KEYS:
            return "[REDACTED]"
        if depth > 3:
            return "[truncated]"
        if isinstance(value, Mapping):
            return {str(k): sanitize(v, str(k), depth + 1) for k, v in value.items()
                    if str(k).lower() not in _PUBLIC_AGENTIC_BLOCKED_KEYS}
        if isinstance(value, (list, tuple, set)):
            return [sanitize(v, None, depth + 1) for v in list(value)[:20]]
        if isinstance(value, str):
            text = value[:300]
            for pattern in _SENSITIVE_TEXT_PATTERNS:
                text = pattern.sub("[REDACTED]", text)
            return text
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:300]

    return [sanitize(dict(item)) for item in trace or [] if isinstance(item, Mapping)]


_PUBLIC_TOOL_TRACE_KEYS = {
    "action_id", "tool", "status", "duration_ms", "retry_count",
    "result_metadata", "error",
}

_PUBLIC_TOOL_ERROR_KEYS = {"code", "error_type", "retry_count", "message"}


def serialize_public_tool_trace(trace: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Strictly serialize tool execution metadata for public API responses.

    This is an allowlist, not a redaction-only serializer: arbitrary tool inputs,
    outputs, evidence, claims, prompts, identities, credentials, and future
    fields are excluded by default. Internal tool traces remain unchanged.
    """
    public: list[dict[str, Any]] = []
    for item in trace or []:
        if not isinstance(item, Mapping):
            continue
        event: dict[str, Any] = {}
        for key in _PUBLIC_TOOL_TRACE_KEYS:
            if key not in item:
                continue
            value = item[key]
            if key == "result_metadata":
                # Only the aggregate counters produced by AgenticToolExecutor.
                if isinstance(value, Mapping):
                    event[key] = {
                        str(k): _safe_value(v)
                        for k, v in value.items()
                        if str(k) in {
                            "raw_result_count", "filtered_already_seen", "success",
                            "source_count", "new_source_count", "hit_count",
                        }
                    }
                else:
                    event[key] = {}
            elif key == "error":
                if isinstance(value, Mapping):
                    event[key] = {
                        str(k): _safe_value(v)
                        for k, v in value.items()
                        if str(k) in _PUBLIC_TOOL_ERROR_KEYS
                    }
                else:
                    event[key] = None
            else:
                event[key] = _safe_value(value)
        public.append(event)
    return public

def serialize_public_trace(trace: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Remove internal identity fields before trace reaches an API response."""
    public: list[dict[str, Any]] = []
    for item in trace or []:
        event = dict(item)
        event.pop("user_id", None)
        event.pop("run_id", None)
        public.append(_safe_value(event))
    return public
