"""Latency / token / cost accounting around any chat model.

``MeteredLLM`` is a transparent proxy: it exposes ``invoke`` exactly like the
LangChain chat model the verification agent already calls, and records what each
call cost. Token counts are taken from what the PROVIDER reported; when a
provider reports nothing they are ``None`` (never estimated from text length), and
cost is ``None`` for any model without a known price.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.bench.models import MODEL_CATALOG

_MAX_RAW_CHARS = 20_000


def _to_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def extract_usage(response: Any) -> tuple[Optional[int], Optional[int]]:
    """(prompt_tokens, completion_tokens) as reported by the provider, else (None, None)."""
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict):
        p, c = _to_int(meta.get("input_tokens")), _to_int(meta.get("output_tokens"))
        if p is not None or c is not None:
            return p, c
    rmeta = getattr(response, "response_metadata", None)
    if isinstance(rmeta, dict):
        usage = rmeta.get("token_usage") or rmeta.get("usage") or {}
        if isinstance(usage, dict):
            return _to_int(usage.get("prompt_tokens")), _to_int(usage.get("completion_tokens"))
    return None, None


def estimate_cost_usd(model: str, prompt_tokens: Optional[int], completion_tokens: Optional[int],
                      catalog: Optional[dict] = None) -> Optional[float]:
    entry = (catalog or MODEL_CATALOG).get(model) or {}
    p_in, p_out = entry.get("price_in"), entry.get("price_out")
    if p_in is None or p_out is None or prompt_tokens is None or completion_tokens is None:
        return None
    return round((prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000, 6)


@dataclass
class UsageMeter:
    calls: list[dict[str, Any]] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        if not self.calls:     # the model was never asked (e.g. the agent abstained): zero is a fact, not a gap
            return {"llm_calls": 0, "llm_failures": 0, "llm_latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "tokens_reported": True}
        ok = [c for c in self.calls if c["ok"]]
        reported = [c for c in ok if c["prompt_tokens"] is not None and c["completion_tokens"] is not None]
        complete = bool(ok) and len(reported) == len(ok)
        p = sum(c["prompt_tokens"] for c in reported) if reported else None
        q = sum(c["completion_tokens"] for c in reported) if reported else None
        return {
            "llm_calls": len(self.calls), "llm_failures": len(self.calls) - len(ok),
            "llm_latency_ms": sum(c["latency_ms"] for c in self.calls),
            "prompt_tokens": p, "completion_tokens": q,
            "total_tokens": (p + q) if p is not None and q is not None else None,
            "tokens_reported": complete,   # False => totals (if any) cover only calls that reported usage
        }


class MeteredLLM:
    """Records every ``invoke`` on ``inner``; behaves otherwise like ``inner``."""

    def __init__(self, inner: Any, meter: Optional[UsageMeter] = None) -> None:
        self._inner = inner
        self.meter = meter or UsageMeter()

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.invoke(messages, *args, **kwargs)
        except Exception as exc:
            self.meter.calls.append({"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                                     "prompt_tokens": None, "completion_tokens": None, "error_type": type(exc).__name__})
            raise
        p, c = extract_usage(response)
        self.meter.calls.append({"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000),
                                 "prompt_tokens": p, "completion_tokens": c, "error_type": None})
        content = getattr(response, "content", response)
        self.meter.raw_responses.append(str(content)[:_MAX_RAW_CHARS])
        return response

    def __getattr__(self, name: str) -> Any:  # delegate anything else (bind_tools, etc.)
        return getattr(self._inner, name)
