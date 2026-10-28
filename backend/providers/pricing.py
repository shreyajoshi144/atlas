"""Cost computation. Costs are estimates from catalog prices (or provider-reported cost when given).

Unknown is never zero: if a price or a usage figure is missing the cost is ``None`` and
``complete`` is False.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .catalog import ModelEntry
from .models import Usage


def resolve_price(entry: ModelEntry, prompt_tokens: int | None, today: date | None = None) -> dict[str, Any] | None:
    """Price row ({in,out,cached_in?}) per 1M tokens effective ``today`` for this prompt size."""
    today = today or date.today()
    active = [s for s in entry.pricing if date.fromisoformat(s["effective_from"]) <= today]
    if not active:
        return None
    sched = max(active, key=lambda s: s["effective_from"])
    for tier in sched["tiers"]:
        cap = tier.get("max_prompt_tokens")
        if cap is None or prompt_tokens is None or prompt_tokens <= cap:
            return tier
    return sched["tiers"][-1]


def tool_price_per_call(entry: ModelEntry) -> float | None:
    tp = entry.tool_prices or {}
    for key in ("web_search_per_1k", "google_search_per_1k"):
        if key in tp:
            return float(tp[key]) / 1000.0
    return None


UNKNOWN_SEARCH_CALL_USD = 0.03    # budgeting only: assumed above every known per-call search price


def Cost(usd, token_cost_usd, tool_cost_usd, complete, basis, price_verified_on, note=None) -> dict[str, Any]:
    """Cost record (plain dict so it serializes as-is)."""
    return {"usd": usd, "token_cost_usd": token_cost_usd, "tool_cost_usd": tool_cost_usd, "complete": complete,
            "basis": basis, "price_verified_on": price_verified_on, "note": note}


def compute_cost(entry: ModelEntry, usage: Usage, today: date | None = None) -> dict[str, Any]:
    if usage.provider_cost_usd is not None:
        return Cost(round(usage.provider_cost_usd, 8), None, None, True, "provider_reported", entry.verified_on,
                    "Cost reported by the provider response.")
    if usage.input_tokens is None or usage.output_tokens is None:
        return Cost(None, None, None, False, "unknown", entry.verified_on, "Provider did not report token usage.")
    price = resolve_price(entry, usage.input_tokens, today)
    if price is None:
        return Cost(None, None, None, False, "unknown", entry.verified_on, "No price known for this model/date.")
    cached = usage.cached_input_tokens or 0
    note = None
    basis = "catalog_price_estimate"
    if cached and "cached_in" in price:
        fresh = max(0, usage.input_tokens - cached)
        in_cost = fresh * price["in"] + cached * price["cached_in"]
    else:
        in_cost = usage.input_tokens * price["in"]
        if cached:
            note = "Cached-input discount not applied (upper bound)."
            basis = "catalog_price_estimate (upper bound: cache discount not applied)"
    token_cost = (in_cost + usage.output_tokens * price["out"]) / 1_000_000
    tool_cost: float | None = 0.0
    complete = True
    if usage.search_calls:
        per = tool_price_per_call(entry)
        if per is None:
            tool_cost, complete = None, False
            note = (note + " " if note else "") + "Search tool fee unknown for this provider, so total cost is incomplete."
        else:
            tool_cost = usage.search_calls * per
            tp = entry.tool_prices.get("google_search_note")
            if tp:
                note = (note + " " if note else "") + tp
    total = None if tool_cost is None else token_cost + tool_cost
    return Cost(None if total is None else round(total, 8), round(token_cost, 8),
                None if tool_cost is None else round(tool_cost, 8), complete,
                basis, entry.verified_on, note)


def estimate_max_cost(entry: ModelEntry, input_tokens: int, max_output_tokens: int, search_calls: int = 0,
                      today: date | None = None, calls: int = 1) -> float | None:
    """Worst-case cost of ONE call; None if the price is unknown (callers must fail closed)."""
    price = resolve_price(entry, input_tokens, today)
    if price is None:
        return None
    cost = (input_tokens * price["in"] + max_output_tokens * price["out"]) / 1_000_000
    if search_calls:
        per = tool_price_per_call(entry)
        cost += search_calls * (per if per is not None else UNKNOWN_SEARCH_CALL_USD)
    return cost * max(1, calls)
