"""Transparent per-call budget guard used by AtlasBench's pinned provider client."""
from __future__ import annotations

import time
import math
from typing import Any

from backend.bench.metering import estimate_cost_usd, extract_usage
from backend.bench.models import allowed_models
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.ops.repository import OperationalRepository, estimate_budget_cost

logger = get_logger(__name__)


def reserved_prompt_tokens(chars: int) -> int:
    """Conservative reservation estimate, not an exact tokenizer count.

    Reserve 25% above one token per represented character plus fixed allowance
    for message/tool framing. Provider tokenization and hidden request overhead can
    still exceed this estimate; configured caps are therefore not hard ceilings.
    """
    return max(1, math.ceil(max(0, chars) * 1.25) + 256)


def prompt_representation_chars(messages: Any) -> int:
    """Count the serialized visible message representation, including roles/framing."""
    return len(str(messages))


class BudgetedLLM:
    def __init__(self, inner: Any, db: Any, *, user_id: str, model: str, max_tokens: int, purpose: str = "benchmark") -> None:
        self._inner = inner
        self._db = db
        self._user_id = user_id
        self._model = model
        self._max_tokens = max_tokens
        self._purpose = purpose
        self._models = allowed_models((get_settings().openai_model,))

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        chars = prompt_representation_chars(messages)
        prompt_tokens = reserved_prompt_tokens(chars)
        estimate = estimate_budget_cost(self._model, prompt_tokens, self._max_tokens, self._models)
        settings = get_settings()
        repository = OperationalRepository(self._db)
        reservation = repository.reserve_call(self._user_id, self._purpose, self._model, estimate,
                                              default_calls=settings.default_daily_llm_call_limit,
                                              default_spend=settings.default_monthly_llm_spend_cap_usd)
        started = time.perf_counter()
        try:
            response = self._inner.invoke(messages, *args, **kwargs)
        except Exception as exc:
            try:
                repository.finish_call(reservation, actual_cost=None, status="failed",
                                       latency_ms=int((time.perf_counter() - started) * 1000), actual_model=self._model,
                                       error_type=type(exc).__name__)
            except Exception as accounting_exc:
                logger.error("Budget usage was not finalized", extra={"error_type": type(accounting_exc).__name__})
            raise
        prompt, completion = extract_usage(response)
        actual = estimate_cost_usd(self._model, prompt, completion, self._models)
        settled_estimate = estimate_budget_cost(self._model, prompt, completion, self._models) \
            if prompt is not None and completion is not None else None
        try:
            repository.finish_call(reservation, actual_cost=actual, status="ok",
                                   latency_ms=int((time.perf_counter() - started) * 1000), actual_model=self._model,
                                   prompt_tokens=prompt, completion_tokens=completion,
                                   estimated_cost=settled_estimate)
        except Exception as exc:
            logger.error("Budget usage was not finalized", extra={"error_type": type(exc).__name__})
        return response

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)
