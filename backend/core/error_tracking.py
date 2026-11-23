"""Optional Sentry error tracking. Off unless SENTRY_DSN is set and sentry-sdk is installed.

Privacy: no PII, no request bodies, and request data is stripped from events, since
Atlas handles user research, reset tokens and provider credentials.
"""
from __future__ import annotations

from backend.core.logging import get_logger

logger = get_logger(__name__)


def _scrub(event, hint):
    request = event.get("request")
    if isinstance(request, dict):
        for field in ("data", "cookies", "headers", "query_string"):
            request.pop(field, None)
    return event


def init_error_tracking(dsn: str | None, environment: str) -> bool:
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return False
    sentry_sdk.init(dsn=dsn, environment=environment, send_default_pii=False,
                    max_request_body_size="never", traces_sample_rate=0.0, before_send=_scrub)
    logger.info("Error tracking enabled")
    return True
