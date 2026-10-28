"""Provider error taxonomy.

Every failure a provider call can produce is mapped to one category so retry,
fallback, circuit-breaker and UI logic never have to parse provider-specific
strings. Messages here are *safe*: they never contain prompts, keys or raw
provider bodies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Categories that are plausibly transient: the same request may succeed later.
RETRYABLE = frozenset({"rate_limit", "overloaded", "server", "timeout", "network"})
# Categories for which trying a *different provider/model* is reasonable.
# Deliberately excludes bad_request (our request is wrong everywhere) and
# content_blocked (never route around another provider's safety decision).
FALLBACK_ELIGIBLE = RETRYABLE | frozenset({"circuit_open", "auth", "quota", "model_not_found", "config"})
# Categories that should push a provider's circuit breaker toward "open".
BREAKER_TRIPPING = frozenset({"overloaded", "server", "timeout", "network"})

_MESSAGES = {
    "auth": ("The provider rejected the server's API key.",
             "Check the key configured on the server for this provider."),
    "quota": ("The provider account has no remaining quota or billing is not set up.",
              "Add credit / check billing with the provider; retrying will not help."),
    "rate_limit": ("The provider is rate limiting requests.", "Wait a moment and try again."),
    "overloaded": ("The provider reported it is overloaded.", "Try again shortly."),
    "server": ("The provider had an internal error.", "Try again shortly."),
    "timeout": ("The provider did not answer in time.", "Try again, or use a faster model."),
    "network": ("Atlas could not reach the provider.", "Check the server's network access."),
    "bad_request": ("The provider rejected the request as invalid.", "Shorten the input or change options."),
    "model_not_found": ("The provider does not offer this model ID for this key.",
                        "The catalog may be out of date; choose another model."),
    "content_blocked": ("The provider declined to answer (safety policy).", "Rephrase the question."),
    "empty": ("The provider returned no answer text.", "Try again or raise the output limit."),
    "truncated": ("The answer hit the output limit before any text was produced.",
                  "Raise the output limit; reasoning models spend part of it on thinking."),
    "parse": ("The provider's response could not be understood.", "This is logged; try again."),
    "budget": ("An account usage limit blocked this call.", "Check the daily/monthly limits."),
    "circuit_open": ("Atlas paused calls to this provider after repeated failures.",
                     "It will be retried automatically in a short while."),
    "config": ("This provider is not configured on the server.", "Ask the operator to set its API key."),
    "cancelled": ("The request was cancelled before it finished.", ""),
    "internal": ("Unexpected internal error.", "This is logged; try again."),
}


@dataclass
class ProviderError(Exception):
    category: str
    http_status: int | None = None
    retry_after: float | None = None          # seconds, from the provider's Retry-After
    attempts: list[dict[str, Any]] = field(default_factory=list)
    provider: str | None = None
    detail_code: str | None = None            # provider's own short error code/type (safe, short)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.category)

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE

    @property
    def message(self) -> str:
        return _MESSAGES.get(self.category, _MESSAGES["internal"])[0]

    @property
    def hint(self) -> str:
        return _MESSAGES.get(self.category, _MESSAGES["internal"])[1]

    def public(self) -> dict[str, Any]:
        """What may be shown to a user. No provider body, no prompt, no key."""
        out = {"category": self.category, "message": self.message, "retryable": self.retryable}
        if self.hint:
            out["hint"] = self.hint
        if self.http_status:
            out["http_status"] = self.http_status
        if self.detail_code:
            out["provider_code"] = self.detail_code[:60]
        return out

    def __str__(self) -> str:  # never include anything sensitive
        return f"ProviderError({self.provider or '?'}:{self.category}:{self.http_status})"


def classify_http(status: int, code: str | None, body_text_lower: str = "") -> str:
    """Map an HTTP status (+ provider error code/type) to a category."""
    c = (code or "").lower()
    t = body_text_lower
    if status in (401,) or c in {"authentication_error", "invalid_api_key", "unauthenticated"}:
        return "auth"
    if status == 403:
        # 403 can be a permission problem (auth) or a region/model entitlement.
        return "auth"
    if status == 402 or c in {"insufficient_quota", "billing_not_active", "credit_balance_too_low"} \
            or "insufficient_quota" in t or "credit balance" in t or "billing" in t and status in (400, 429):
        return "quota"
    if status == 429 or c in {"rate_limit_error", "rate_limit_exceeded", "resource_exhausted"}:
        return "rate_limit"
    if status in (503, 529) or c in {"overloaded_error", "unavailable"}:
        return "overloaded"
    if status == 404 or c in {"model_not_found", "not_found_error", "not_found"}:
        return "model_not_found"
    if status in (408, 504) or c in {"timeout_error", "deadline_exceeded"}:
        return "timeout"
    if status >= 500:
        return "server"
    if status in (400, 413, 422) or c in {"invalid_request_error", "invalid_argument", "request_too_large"}:
        return "bad_request"
    return "bad_request" if 400 <= status < 500 else "internal"
