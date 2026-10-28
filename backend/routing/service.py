"""Routing service behind the API: ownership-checked policies, decision preview, usage reporting.

Every operation takes the authenticated ``user_id``. A policy that does not exist *or belongs to
someone else* raises the same ``RoutingNotFound``. Preview never calls a model.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from backend.bench.models import EVALUATOR_VERSION
from backend.bench.repository import BenchRepository
from backend.database import ResearchRepository
from backend.routing.evidence import load_evidence
from backend.routing.models import (
    CHARS_PER_TOKEN, DEFAULT_COMPLETION_TOKENS_ESTIMATE, EVIDENCE_STRATEGIES, MAX_CANDIDATES, MAX_FALLBACK_MODELS, MAX_POLICIES_PER_USER,
    MAX_USAGE_ROWS_PER_USER, RoutingPolicy, check_models,
)
from backend.routing.failures import CATEGORIES
from backend.routing.repository import RoutingRepository
from backend.routing.router import ModelRouter


def _j(text: Optional[str], default: Any = None) -> Any:
    return json.loads(text) if text else default


class RoutingService:
    def __init__(self, repository: ResearchRepository, *, router: Optional[ModelRouter] = None, clock: Optional[Callable[[], datetime]] = None) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.router = router or ModelRouter(repository_factory=lambda: repository, clock=self._clock)
        self.repo = RoutingRepository(repository.db)
        self.bench = BenchRepository(repository.db)

    # ── views ────────────────────────────────────────────────────────────────
    def _policy_view(self, row: dict[str, Any], active_id: Optional[str]) -> dict[str, Any]:
        p = RoutingPolicy.model_validate(_j(row["policy_json"]))
        return {"policy_id": row["policy_id"], "created_at": row["created_at"], "active": row["policy_id"] == active_id, "policy_hash": row["policy_hash"], **p.model_dump(mode="json")}

    def _active_id(self, user_id: str) -> Optional[str]:
        row = self.repo.active_policy_row(user_id)
        return row["policy_id"] if row else None

    def overview(self, user_id: str) -> dict[str, Any]:
        allowed = self.router.allowed()
        probe = RoutingPolicy(name="probe", mode="autopilot", model=self.router.default_model(), min_evidence_cases=1)
        evidence = load_evidence(self.bench, user_id, probe, self._clock())
        active = self.repo.active_policy_row(user_id)
        rows, _ = self.repo.list_usage(user_id, limit=1)
        return {
            "default_model": self.router.default_model(), "evaluator_version": EVALUATOR_VERSION,
            "models": [{"model": m, "label": v["label"], "price_in": v["price_in"], "price_out": v["price_out"],
                        "evidence": evidence[m].public() if m in evidence else None} for m, v in sorted(allowed.items())],
            "active_policy": self._policy_view(active, active["policy_id"]) if active else None,
            "fallback": {"categories": list(CATEGORIES), "max_fallback_models": MAX_FALLBACK_MODELS, "never_during_benchmarks": True},
            "limits": {"max_policies": MAX_POLICIES_PER_USER, "max_candidates": MAX_CANDIDATES, "max_usage_rows": MAX_USAGE_ROWS_PER_USER},
            "evidence_strategies": list(EVIDENCE_STRATEGIES),
            "estimates": {"chars_per_token": CHARS_PER_TOKEN, "default_completion_tokens": DEFAULT_COMPLETION_TOKENS_ESTIMATE},
            "counts": {"policies": len(self.repo.list_policy_rows(user_id)), "usage_rows": self.repo.list_usage(user_id, limit=1)[1], "has_usage": bool(rows)},
        }

    # ── policies ─────────────────────────────────────────────────────────────
    def list_policies(self, user_id: str) -> dict[str, Any]:
        active = self._active_id(user_id)
        items = [self._policy_view(r, active) for r in self.repo.list_policy_rows(user_id)]
        return {"items": items, "total": len(items), "active_policy_id": active, "default_model": self.router.default_model()}

    def create_policy(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        policy = RoutingPolicy.model_validate(payload)
        check_models(policy, self.router.allowed())
        row = self.repo.create_policy(user_id, policy, self._clock())
        return self._policy_view(row, self._active_id(user_id))

    def delete_policy(self, user_id: str, policy_id: str) -> None:
        self.repo.delete_policy(user_id, policy_id)

    def set_active(self, user_id: str, policy_id: Optional[str]) -> dict[str, Any]:
        self.repo.set_active(user_id, policy_id, self._clock())
        return self.list_policies(user_id)

    # ── preview (no model is called) ─────────────────────────────────────────
    def preview(self, user_id: str, *, policy_id: Optional[str] = None, policy: Optional[dict[str, Any]] = None, prompt_tokens: int = 2000) -> dict[str, Any]:
        if policy_id and policy:
            raise ValueError("Provide a policy_id or an inline policy, not both.")
        if policy:
            chosen = RoutingPolicy.model_validate(policy)
            check_models(chosen, self.router.allowed())
            source = "inline"
        elif policy_id:
            chosen = RoutingPolicy.model_validate(_j(self.repo.get_policy_row(user_id, policy_id)["policy_json"]))
            source = "saved"
        else:
            pid, chosen = self.router.active_policy(user_id)
            source = "active" if pid else "default"
        decision = self.router.decide(user_id, prompt_tokens, chosen)
        return {"source": source, "policy_name": chosen.name, "mode": chosen.mode, "prompt_tokens_assumed": prompt_tokens,
                "fallback": {"enabled": chosen.fallback_enabled, "models": chosen.fallback_models}, "decision": decision.public()}

    # ── usage ────────────────────────────────────────────────────────────────
    @staticmethod
    def _usage_view(r: dict[str, Any]) -> dict[str, Any]:
        attempts = _j(r["attempts_json"], [])
        # fallback_used remains the compatibility signal for a successful
        # fallback. These additive fields make failed/exhausted fallback
        # chains distinguishable without inferring success from attempted calls.
        fallback_attempted = len(attempts) > 1
        fallback_succeeded = bool(r["fallback_used"])
        fallback_failed = fallback_attempted and r["status"] == "failed"
        return {"usage_id": r["usage_id"], "created_at": r["created_at"], "run_ref": r["run_ref"], "purpose": r["purpose"], "policy_id": r["policy_id"],
                "policy_name": r["policy_name"], "mode": r["mode"], "reason_code": r["reason_code"], "reason": r["reason"],
                "requested_model": r["requested_model"], "actual_model": r["actual_model"], "status": r["status"], "fallback_used": bool(r["fallback_used"]),
                "fallback_attempted": fallback_attempted, "fallback_succeeded": fallback_succeeded, "fallback_failed": fallback_failed,
                "fallback_reason": r["fallback_reason"], "attempts": attempts, "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"], "total_tokens": r["total_tokens"], "tokens_reported": bool(r["tokens_reported"]),
                "est_cost_usd": r["est_cost_usd"],
                # Backwards-compatible legacy field; this is a catalog-price estimate
                # based on provider-reported token counts, not a provider-billed charge.
                "actual_cost_usd": r["actual_cost_usd"],
                "usage_cost_estimate_usd": r["actual_cost_usd"], "provider_reported_cost_usd": None,
                "latency_ms": r["latency_ms"], "llm_latency_ms": r["llm_latency_ms"],
                "error_type": r["error_type"], "evidence_run_id": r["evidence_run_id"]}

    def list_usage(self, user_id: str, **filters: Any) -> dict[str, Any]:
        rows, total = self.repo.list_usage(user_id, **filters)
        return {"items": [self._usage_view(r) for r in rows], "total": total, "limit": max(1, min(int(filters.get("limit", 25)), 100)),
                "offset": max(0, int(filters.get("offset", 0)))}

    def usage_summary(self, user_id: str, days: int = 30) -> dict[str, Any]:
        since = (self._clock() - timedelta(days=days)).astimezone(timezone.utc).isoformat()
        by_model = []
        for r in self.repo.usage_summary(user_id, since):
            ok_calls = r["calls"] - (r["failed"] or 0)
            by_model.append({
                "model": r["model"], "calls": r["calls"], "failed": r["failed"] or 0, "fallbacks": r["fallbacks"] or 0,
                "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"], "total_tokens": r["total_tokens"],
                "tokens_complete": ok_calls > 0 and (r["calls_with_tokens"] or 0) == ok_calls,
                "cost_usd": r["cost_usd"], "cost_complete": ok_calls > 0 and (r["calls_with_cost"] or 0) == ok_calls,
                "est_cost_usd": r["est_cost_usd"], "avg_latency_ms": round(r["avg_latency_ms"]) if r["avg_latency_ms"] is not None else None,
                "max_latency_ms": r["max_latency_ms"]})
        calls = sum(m["calls"] for m in by_model)
        costs = [m["cost_usd"] for m in by_model if m["cost_usd"] is not None]
        total = {
            "calls": calls, "failed": sum(m["failed"] for m in by_model), "fallbacks": sum(m["fallbacks"] for m in by_model),
            "fallback_rate": round(sum(m["fallbacks"] for m in by_model) / calls, 4) if calls else None,
            "total_tokens": sum(m["total_tokens"] for m in by_model if m["total_tokens"] is not None) if any(m["total_tokens"] is not None for m in by_model) else None,
            "cost_usd": round(sum(costs), 6) if costs else None, "cost_complete": bool(by_model) and all(m["cost_complete"] for m in by_model),
            "est_cost_usd": round(sum(m["est_cost_usd"] for m in by_model if m["est_cost_usd"] is not None), 6) if any(m["est_cost_usd"] is not None for m in by_model) else None,
            "avg_latency_ms": round(sum((m["avg_latency_ms"] or 0) * m["calls"] for m in by_model) / calls) if calls else None}
        return {"days": days, "since": since, "total": total, "by_model": by_model}
