from __future__ import annotations

import traceback

from fastapi import HTTPException

from backend.core.logging import get_logger, safe_traceback_frames

logger = get_logger(__name__)


def _debug_diagnostics_enabled() -> bool:
    """Full diagnostics only when explicitly running as development with DEBUG on."""
    try:
        from backend.core.config import get_settings

        settings = get_settings()
        return bool(settings.debug) and settings.app_env == "development"
    except Exception:
        return False


def safe_server_error(route_logger, action: str, exc: Exception, status_code: int = 500) -> HTTPException:
    """Record safe diagnostics and return a non-sensitive error to the client.

    Exception messages and formatted tracebacks are deliberately excluded:
    provider and database exceptions can contain credentials or user content.
    Sanitized stack locations and the exception type retain useful diagnostics.
    """
    extra = {
        "error_type": type(exc).__name__,
        "stack_frames": safe_traceback_frames(exc.__traceback__),
    }
    if _debug_diagnostics_enabled():
        # Local development only: the message/traceback go to the server log, never the response.
        extra["debug_error_message"] = str(exc)[:1000]
        extra["debug_traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
    route_logger.error(f"{action} failed", extra=extra)
    return HTTPException(status_code=status_code, detail=f"{action} failed. Please try again.")
