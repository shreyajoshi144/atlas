"""The router: picks a model per call, falls back on provider failure, and records everything.

Behaviour per call, by scope (see ``context``):

* no scope      → legacy passthrough: the application model, no fallback, nothing recorded.
* benchmark     → the pinned model only. No autopilot, no fallback, nothing recorded here
                  (AtlasBench meters its own calls). This keeps model comparisons valid.
* user scope    → the user's active policy decides the model (explicit or autopilot); on a
                  provider failure (timeout / 429 / 5xx / unreachable) and only if the policy
                  allows it, the next model in the policy's fallback list is tried.

Every user-scope call writes one ``llm_usage`` row: requested vs actual model, why, whether and why a
fallback happened, every attempt, tokens, estimated and actual cost, latency. A fallback is never
silent: it is in that row AND in the application log. Failure to record never breaks the call.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Optional

from backend.bench.metering import estimate_cost_usd, extract_usage
from backend.bench.repository import BenchRepository
from backend.core.logging import get_logger
from backend.core.config import get_settings
from backend.ops.repository import OperationalRepository, UsageLimitExceeded, estimate_budget_cost
from backend.ops.limits import prompt_representation_chars, reserved_prompt_tokens
from backend.routing import selector
from backend.routing.context import RouteScope, current_scope
from backend.routing.evidence import load_evidence
from backend.routing.failures import classify_failure
from backend.routing.models import RoutingPolicy, allowed_models, default_policy
from backend.routing.repository import RoutingRepository

logger = get_logger(__name__)
Derive = Callable[[Any], Any]


def _identity(client: Any) -> Any:
    return client


class ModelRouter:
    def __init__(self, *, repository_factory: Optional[Callable[[], Any]] = None, client_factory: Optional[Callable[[str], Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None, default_model: Optional[str] = None) -> None:
        self._repository_factory = repository_factory or _default_repository
        self._client_factory = client_factory or _default_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._default_model = default_model
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── configuration ────────────────────────────────────────────────────────
    def default_model(self) -> str:
        if self._default_model:
            return self._default_model
        from backend.core.config import get_settings

        return get_settings().openai_model

    def allowed(self) -> dict[str, Any]:
        return allowed_models((self.default_model(),))

    def client(self, model: str) -> Any:
        with self._lock:
            if model not in self._clients:
                self._clients[model] = self._client_factory(model)
            return self._clients[model]

    def repos(self) -> tuple[RoutingRepository, BenchRepository]:
        db = self._repository_factory().db
        return RoutingRepository(db), BenchRepository(db)

    # ── decisions ────────────────────────────────────────────────────────────
    def active_policy(self, user_id: str) -> tuple[Optional[str], RoutingPolicy]:
        routing, _ = self.repos()
        row = routing.active_policy_row(user_id)
        if row is None:
            return None, default_policy(self.default_model())
        try:
            return row["policy_id"], RoutingPolicy.model_validate(json.loads(row["policy_json"]))
        except Exception as exc:  # a corrupt row must not take the product down — but say so
            logger.warning("Routing policy unreadable; using the application default", extra={"policy_id": row["policy_id"], "error_type": type(exc).__name__})
            return None, default_policy(self.default_model())

    def decide(self, user_id: str, prompt_tokens: int, policy: Optional[RoutingPolicy] = None) -> selector.Decision:
        """The model choice for one call (also used by the preview endpoint — no model is called)."""
        allowed = self.allowed()
        if policy is None:
            _, policy = self.active_policy(user_id)
        evidence = {}
        if policy.mode == "autopilot":
            _, bench = self.repos()
            evidence = load_evidence(bench, user_id, policy, self._clock())
        decision = selector.decide(policy, allowed, evidence, prompt_tokens)
        if decision.requested_model not in allowed:      # e.g. the application default changed since the policy was written
            decision = selector.Decision(decision.mode, self.default_model(), "model_not_allowed",
                                         f"Policy model {decision.requested_model} is no longer allowed. Using the application default {self.default_model()}.", None)
        return decision

    # ── the call ─────────────────────────────────────────────────────────────
    def invoke(self, messages: Any, derive: Derive, args: tuple, kwargs: dict) -> Any:
        scope = current_scope()
        if scope is None or (scope.user_id is None and not scope.benchmark):
            return derive(self.client(self.default_model())).invoke(messages, *args, **kwargs)        # legacy passthrough
        if scope.benchmark:
            if not scope.pinned_model or scope.pinned_model not in self.allowed():
                raise ValueError(f"Pinned benchmark model '{scope.pinned_model}' is not allowed; refusing to substitute another model.")
            model = scope.pinned_model
            reservation, reserved_cost = self._reserve_budget(scope, [model], messages)
            started = time.perf_counter()
            try:
                response = derive(self.client(model)).invoke(messages, *args, **kwargs)
            except Exception as exc:
                self._finish_budget(reservation, reserved_cost, None, started, "failed", model=model, error_type=type(exc).__name__)
                raise
            prompt_tokens, completion_tokens = extract_usage(response)
            measured_cost = (estimate_budget_cost(model, prompt_tokens, completion_tokens, self.allowed())
                             if prompt_tokens is not None and completion_tokens is not None else None)
            self._finish_budget(reservation, reserved_cost, measured_cost,
                                started, "ok", model=model, response=response, estimated_cost=measured_cost)
            return response                                                # pinned: no autopilot, no fallback
        return self._routed_invoke(scope, messages, derive, args, kwargs)

    def _reserve_budget(self, scope: RouteScope, models: list[str], messages: Any) -> tuple[Optional[str], float]:
        if not scope.user_id:
            return None, 0.0
        chars = prompt_representation_chars(messages)
        prompt_tokens = reserved_prompt_tokens(chars)
        settings = get_settings()
        # Reserve the full configured output allowance for every explicit
        # fallback candidate. This is conservative and prevents an unpriced
        # fallback chain from bypassing a configured spend cap.
        estimates = self._chain_estimates(models, prompt_tokens)
        estimate: Optional[float]
        if any(value is None for value in estimates.values()):
            estimate = None
        else:
            estimate = sum(float(value) for value in estimates.values() if value is not None)
        try:
            db = self._repository_factory().db
            reservation = OperationalRepository(db).reserve_call(
                scope.user_id, scope.purpose, ",".join(models), estimate,
                default_calls=settings.default_daily_llm_call_limit,
                default_spend=settings.default_monthly_llm_spend_cap_usd,
                now=self._clock(),
            )
            return reservation, float(estimate or 0.0)
        except UsageLimitExceeded:
            raise
        except Exception as exc:
            logger.error("Usage budget check failed; model call blocked", extra={"error_type": type(exc).__name__})
            raise UsageLimitExceeded("Usage controls are temporarily unavailable; model call blocked.") from exc

    def _chain_estimates(self, models: list[str], prompt_tokens: int | None = None) -> dict[str, float | None]:
        if prompt_tokens is None:
            prompt_tokens = reserved_prompt_tokens(0)
        settings = get_settings()
        return {model: estimate_budget_cost(model, prompt_tokens, settings.openai_max_tokens, self.allowed())
                for model in models}

    def _finish_budget(self, reservation: Optional[str], reserved_cost: float, actual_cost: Optional[float],
                       started: float, status: str, *, model: str | None = None, response: Any = None,
                       error_type: str | None = None, estimated_cost: float | None = None) -> None:
        if reservation is None:
            return
        try:
            db = self._repository_factory().db
            prompt_tokens, completion_tokens = extract_usage(response) if response is not None else (None, None)
            OperationalRepository(db).finish_call(
                reservation, actual_cost=actual_cost, status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                actual_model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                error_type=error_type, estimated_cost=estimated_cost,
            )
        except Exception as exc:
            logger.error("Model budget usage was not finalized", extra={"error_type": type(exc).__name__, "reserved_cost_usd": reserved_cost})

    def _routed_invoke(self, scope: RouteScope, messages: Any, derive: Derive, args: tuple, kwargs: dict) -> Any:
        user_id = scope.user_id
        chars = sum(len(str(getattr(m, "content", m))) for m in (messages if isinstance(messages, (list, tuple)) else [messages]))
        prompt_est = selector.estimate_prompt_tokens(chars)
        policy_id: Optional[str] = None
        allowed = self.allowed()
        if scope.manual_model is not None:
            if scope.manual_model not in allowed:
                raise ValueError(f"Playground model '{scope.manual_model}' is not allowed; refusing to substitute another model.")
            policy_id = None
            policy = default_policy(scope.manual_model)
            decision = selector.Decision("explicit", scope.manual_model, "playground_manual",
                                         f"Manually selected in AI Playground: {scope.manual_model}. Policy fallback is not used for this explicit comparison.",
                                         selector.est_call_cost(scope.manual_model, allowed, prompt_est, None))
            chain = [scope.manual_model]
        else:
            try:
                policy_id, policy = self.active_policy(user_id)
                decision = self.decide(user_id, prompt_est, policy)
            except Exception as exc:
                logger.warning("Routing unavailable; using the application default", extra={"error_type": type(exc).__name__})
                policy = default_policy(self.default_model())
                decision = selector.Decision("explicit", self.default_model(), "routing_unavailable",
                                             f"Routing could not read its policy or evidence ({type(exc).__name__}). Using the application default.", None)
            chain = [decision.requested_model]
            if policy.fallback_enabled:
                chain += [m for m in policy.fallback_models if m in allowed and m not in chain]

        reservation, reserved_cost = self._reserve_budget(scope, chain, messages)
        prompt_tokens_reserved = reserved_prompt_tokens(prompt_representation_chars(messages))
        per_model_reserve = self._chain_estimates(chain, prompt_tokens_reserved)

        attempts: list[dict[str, Any]] = []
        conservative_attempt_costs: list[float] = []
        measured_success_costs: list[float] = []
        costs_complete = True
        started = time.perf_counter()
        last_exc: Optional[BaseException] = None
        for i, model in enumerate(chain):
            t0 = time.perf_counter()
            try:
                response = derive(self.client(model)).invoke(messages, *args, **kwargs)
            except Exception as exc:
                failure = classify_failure(exc)
                attempts.append({"model": model, "outcome": "failed", "category": failure.category if failure else "not_eligible_for_fallback",
                                 "status_code": failure.status_code if failure else getattr(exc, "status_code", None),
                                 "error_type": type(exc).__name__, "latency_ms": int((time.perf_counter() - t0) * 1000)})
                last_exc = exc
                attempt_reserve = per_model_reserve[model]
                if attempt_reserve is None:
                    costs_complete = False
                else:
                    conservative_attempt_costs.append(attempt_reserve)
                if failure is not None and i + 1 < len(chain):
                    logger.warning("Model fallback", extra={"requested_model": chain[0], "failed_model": model, "next_model": chain[i + 1],
                                                            "reason": failure.describe(), "user_id": user_id, "purpose": scope.purpose})
                    continue
                reason = None
                if failure is not None:
                    reason = ("Fallback exhausted: every model in the chain failed." if len(chain) > 1
                              else ("Fallback is disabled in this policy." if not policy.fallback_enabled else "Fallback is enabled but the policy lists no other model."))
                settled_estimate = sum(conservative_attempt_costs) if costs_complete else None
                self._finish_budget(reservation, reserved_cost, None, started, "failed",
                                    model=attempts[-1].get("model"), error_type=type(exc).__name__,
                                    estimated_cost=settled_estimate)
                self._record(scope, decision, policy, policy_id, attempts, None, started, reason)
                raise
            attempts.append({"model": model, "outcome": "ok", "category": None, "status_code": None, "error_type": None,
                             "latency_ms": int((time.perf_counter() - t0) * 1000)})
            p_tokens, c_tokens = extract_usage(response)
            measured_cost = (estimate_budget_cost(model, p_tokens, c_tokens, allowed)
                             if p_tokens is not None and c_tokens is not None else None)
            if measured_cost is None:
                attempt_reserve = per_model_reserve[model]
                if attempt_reserve is None:
                    costs_complete = False
                else:
                    conservative_attempt_costs.append(attempt_reserve)
            else:
                measured_success_costs.append(measured_cost)
                conservative_attempt_costs.append(measured_cost)
            settled_estimate = sum(conservative_attempt_costs) if costs_complete else None
            actual_total = sum(measured_success_costs) if measured_success_costs else None
            self._finish_budget(reservation, reserved_cost, actual_total, started, "ok", model=model, response=response,
                                estimated_cost=settled_estimate)
            self._record(scope, decision, policy, policy_id, attempts, response, started, None)
            return response
        raise last_exc  # type: ignore[misc]  (unreachable: the loop returns or raises)

    # ── recording ────────────────────────────────────────────────────────────
    def _record(self, scope: RouteScope, decision: selector.Decision, policy: RoutingPolicy, policy_id: Optional[str], attempts: list[dict[str, Any]],
                response: Any, started: float, failure_note: Optional[str]) -> None:
        try:
            ok = response is not None and attempts[-1]["outcome"] == "ok"
            actual = attempts[-1]["model"] if ok else None
            p, c = extract_usage(response) if ok else (None, None)
            reported = p is not None and c is not None
            failed = [a for a in attempts if a["outcome"] == "failed"]
            fallback_used = bool(ok and failed)
            if fallback_used:
                fallback_reason = " → ".join(f"{a['model']}: {a['category'].replace('_', ' ')}" + (f" (HTTP {a['status_code']})" if a.get("status_code") else "") for a in failed) + f" → {actual}"
            else:
                fallback_reason = failure_note
            routing, _ = self.repos()
            routing.insert_usage(scope.user_id, {
                "run_ref": scope.run_ref, "purpose": scope.purpose, "policy_id": policy_id, "policy_name": policy.name, "mode": decision.mode,
                "reason_code": decision.reason_code, "reason": decision.reason, "requested_model": decision.requested_model, "actual_model": actual,
                "status": "ok" if ok else "failed", "fallback_used": 1 if fallback_used else 0, "fallback_reason": fallback_reason,
                "attempts_json": json.dumps(attempts), "prompt_tokens": p, "completion_tokens": c, "total_tokens": (p + c) if reported else None,
                "tokens_reported": 1 if reported else 0, "est_cost_usd": decision.est_cost_usd,
                "actual_cost_usd": estimate_cost_usd(actual, p, c, self.allowed()) if ok and reported else None,
                "latency_ms": int((time.perf_counter() - started) * 1000), "llm_latency_ms": attempts[-1]["latency_ms"] if ok else None,
                "error_type": None if ok else attempts[-1].get("error_type"), "evidence_run_id": decision.evidence_run_id}, self._clock())
        except Exception as exc:    # accounting must never break a user's request, but must never fail quietly either
            logger.warning("Model usage was NOT recorded", extra={"error_type": type(exc).__name__})


class RoutedLLM:
    """Drop-in for the chat model the agents already use: ``invoke`` and ``with_structured_output``."""

    def __init__(self, router: ModelRouter, derive: Derive = _identity) -> None:
        self._router = router
        self._derive = derive

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        return self._router.invoke(messages, self._derive, args, kwargs)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "RoutedLLM":
        prev = self._derive
        return RoutedLLM(self._router, lambda client: prev(client).with_structured_output(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:   # e.g. model_name: answer from the application model so callers keep working
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._derive(self._router.client(self._router.default_model())), name)


def _default_repository() -> Any:
    from backend.database import ResearchRepository

    return ResearchRepository()


def _default_client(model: str) -> Any:
    from backend.core.config import get_settings
    from backend.core.llm import build_chat_llm, get_default_chat_llm

    s = get_settings()
    if model == s.openai_model:
        return get_default_chat_llm()
    return build_chat_llm(model, temperature=s.openai_temperature, max_tokens=s.openai_max_tokens)


@lru_cache(maxsize=1)
def get_router() -> ModelRouter:
    return ModelRouter()


@lru_cache(maxsize=1)
def get_routed_llm() -> RoutedLLM:
    return RoutedLLM(get_router())
