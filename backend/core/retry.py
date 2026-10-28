"""Bounded retry policy for transient provider rate-limit responses.

Payload/validation errors are not retried because repeating an identical request
cannot make it smaller or more valid. Provider routing handles eligible fallback
separately; this helper only retries explicit rate-limit signals.
"""
from __future__ import annotations

import time
from typing import Any

from backend.core.logging import get_logger

logger = get_logger(__name__)


def invoke_with_retry(runnable: Any, messages: Any, max_retries: int = 2,
                      base_delay_seconds: float = 20.0, *,
                      invoke_kwargs: dict[str, Any] | None = None) -> Any:
    """``invoke_kwargs`` are per-call model options (e.g. ``max_tokens``, ``timeout``)."""
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    if base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be non-negative")

    for attempt in range(max_retries + 1):
        try:
            return runnable.invoke(messages, **(invoke_kwargs or {}))
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            message = str(exc).lower()
            is_rate_limit = status_code == 429 or (status_code is None and ("rate_limit" in message or "rate limit" in message))
            if not is_rate_limit or attempt == max_retries:
                raise
            delay = base_delay_seconds * (attempt + 1)
            # Provider exception text can contain request details or credentials;
            # keep the log useful without copying untrusted raw error bodies.
            logger.warning(
                "LLM call hit a rate limit; retrying",
                extra={"attempt": attempt + 1, "max_retries": max_retries,
                       "delay_seconds": delay, "error_type": type(exc).__name__,
                       "status_code": status_code},
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")  # pragma: no cover
