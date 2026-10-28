"""Which provider failures justify trying another model.

Fallback exists to ride out *provider-side* trouble: a timeout, rate limiting (429), a 5xx or an
unreachable provider (and a model that has been withdrawn). It must never mask *our* mistakes:
a malformed request (400), bad credentials (401/403), a validation or parsing error — those
would fail identically on any model, so they propagate untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CATEGORIES = ("rate_limited", "timeout", "provider_error", "provider_unreachable", "model_unavailable")
_CONNECTION_NAMES = ("apiconnectionerror", "connecterror", "connectionerror", "readerror", "remoteprotocolerror", "networkerror")
_UNAVAILABLE_MARKERS = ("model_decommissioned", "model_not_found", "decommissioned", "does not exist")


@dataclass(frozen=True)
class Failure:
    category: str
    status_code: Optional[int] = None

    def describe(self) -> str:
        return f"{self.category.replace('_', ' ')}" + (f" (HTTP {self.status_code})" if self.status_code else "")


def status_of(exc: BaseException) -> Optional[int]:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def classify_failure(exc: BaseException) -> Optional[Failure]:
    """``Failure`` if falling back is justified, else ``None`` (the error must propagate)."""
    status = status_of(exc)
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if status == 429 or "rate_limit" in msg or "rate limit" in msg:
        return Failure("rate_limited", status or 429)
    if status == 408 or isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in msg:
        return Failure("timeout", status)
    if status is not None and status >= 500:
        return Failure("provider_error", status)
    if status in (400, 404) and any(m in msg for m in _UNAVAILABLE_MARKERS):
        return Failure("model_unavailable", status)
    if isinstance(exc, ConnectionError) or any(n in name for n in _CONNECTION_NAMES):
        return Failure("provider_unreachable", status)
    return None
