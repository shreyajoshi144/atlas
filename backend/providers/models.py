"""Normalized request/response types shared by every provider adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    system: str
    user: str
    max_output_tokens: int
    native_search: bool = False           # use the provider's own web-search tool
    json_schema: dict[str, Any] | None = None   # native structured output when supported
    request_id: str = ""


@dataclass
class Citation:
    kind: str                              # "provider_web" (provider-reported); atlas citations are derived later
    url: str | None = None
    title: str | None = None
    snippet: str | None = None             # provider-supplied cited text, trimmed

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "title": self.title, "snippet": self.snippet}


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None       # billed output, INCLUDING reasoning/thinking when the provider reports it
    reasoning_tokens: int | None = None    # informational subset (None when not reported separately)
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    search_calls: int = 0                  # provider-side search/tool invocations observed
    provider_cost_usd: float | None = None # only when the provider itself reports a cost

    @property
    def reported(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    def to_dict(self) -> dict[str, Any]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens, "cached_input_tokens": self.cached_input_tokens,
                "total_tokens": self.total_tokens, "reported": self.reported}


@dataclass
class NormalizedResponse:
    text: str
    finish_reason: str                      # stop | length | content_filter | refusal | tool_limit | other
    usage: Usage
    citations: list[Citation] = field(default_factory=list)
    truncated: bool = False
    provider: str = ""
    model: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    total_ms: int | None = None
    served_model: str | None = None         # model string echoed by the provider, if any

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)
