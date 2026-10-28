"""HTTP request correlation context for structured logs.

Only a short, header-safe caller-supplied request ID is accepted. Invalid or
oversized values are replaced with a generated UUID; the ID is returned in
``X-Request-ID`` so clients can correlate a response with server logs.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from backend.core.logging import request_id_ctx, session_id_ctx

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


async def request_context_middleware(request: Any, call_next: Any) -> Any:
    supplied = request.headers.get("x-request-id", "")
    request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
    request_token = request_id_ctx.set(request_id)
    # Session identity is set by application code when it is known. Clear any
    # inherited value for this request so a reused async context cannot leak it.
    session_token = session_id_ctx.set("-")
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        session_id_ctx.reset(session_token)
        request_id_ctx.reset(request_token)
