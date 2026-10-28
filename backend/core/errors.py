from __future__ import annotations

from fastapi import HTTPException

from backend.core.logging import get_logger, safe_traceback_frames

logger = get_logger(__name__)


def safe_server_error(route_logger, action: str, exc: Exception, status_code: int = 500) -> HTTPException:
    """Record safe diagnostics and return a non-sensitive error to the client.

    Exception messages and formatted tracebacks are deliberately excluded:
    provider and database exceptions can contain credentials or user content.
    Sanitized stack locations and the exception type retain useful diagnostics.
    """
    route_logger.error(
        f"{action} failed",
        extra={
            "error_type": type(exc).__name__,
            "stack_frames": safe_traceback_frames(exc.__traceback__),
        },
    )
    return HTTPException(status_code=status_code, detail=f"{action} failed. Please try again.")
