"""Pure model selection. No I/O, no clock, no network — everything it needs is passed in.

Autopilot rule: among the candidate models, keep those that meet ALL of
  quality  ≥ policy.min_quality                      (AtlasBench composite, failures counted as 0)
  latency  ≤ policy.max_latency_ms                   (AtlasBench mean time per model call)
  cost     ≤ policy.max_cost_per_call_usd            (price × estimated tokens of THIS call)
then pick the cheapest (ties: higher quality, lower latency, then name — deterministic).
A model that cannot be checked against a requirement (no price, no benchmark, no latency figure) is
not eligible: "unknown" never counts as "good enough". If nothing qualifies, ``policy.model`` is used and
the decision says why. Costs here are ESTIMATES made before the call.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.bench.metering import estimate_cost_usd
from backend.routing.evidence import ModelEvidence
from backend.routing.models import CHARS_PER_TOKEN, DEFAULT_COMPLETION_TOKENS_ESTIMATE, RoutingPolicy


def estimate_prompt_tokens(chars: int) -> int:
    return max(1, math.ceil(max(0, chars) / CHARS_PER_TOKEN))


@dataclass
class Candidate:
    model: str
    est_cost_usd: Optional[float]
    evidence: Optional[ModelEvidence]
    eligible: bool = False
    rejected: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"model": self.model, "est_cost_usd": self.est_cost_usd, "eligible": self.eligible, "rejected": self.rejected,
                "evidence": self.evidence.public() if self.evidence else None}


@dataclass
class Decision:
    mode: str
    requested_model: str
    reason_code: str
    reason: str
    est_cost_usd: Optional[float]
    evidence_run_id: Optional[str] = None
    candidates: list[Candidate] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"mode": self.mode, "requested_model": self.requested_model, "reason_code": self.reason_code, "reason": self.reason,
                "est_cost_usd": self.est_cost_usd, "evidence_run_id": self.evidence_run_id, "candidates": [c.public() for c in self.candidates]}


def est_call_cost(model: str, allowed: dict[str, Any], prompt_tokens: int, evidence: Optional[ModelEvidence]) -> Optional[float]:
    completion = evidence.avg_completion_tokens if evidence and evidence.avg_completion_tokens is not None else DEFAULT_COMPLETION_TOKENS_ESTIMATE
    return estimate_cost_usd(model, prompt_tokens, int(round(completion)), allowed)


def _usd(x: Optional[float]) -> str:
    return "unknown" if x is None else (f"${x:.4f}" if x < 0.01 else f"${x:.3f}")


def decide(policy: RoutingPolicy, allowed: dict[str, Any], evidence: dict[str, ModelEvidence], prompt_tokens: int) -> Decision:
    if policy.mode == "explicit":
        return Decision("explicit", policy.model, "explicit", f"Explicit selection: {policy.model} (policy “{policy.name}”).",
                        est_call_cost(policy.model, allowed, prompt_tokens, evidence.get(policy.model)))

    cands: list[Candidate] = []
    for model in policy.pool(allowed):
        ev = evidence.get(model)
        c = Candidate(model, est_call_cost(model, allowed, prompt_tokens, ev), ev)
        if model not in allowed:
            c.rejected.append("not an allowed model")
        else:
            if c.est_cost_usd is None:
                c.rejected.append("price unknown, so cost cannot be checked")
            elif c.est_cost_usd > policy.max_cost_per_call_usd:
                c.rejected.append(f"estimated {_usd(c.est_cost_usd)} per call > limit {_usd(policy.max_cost_per_call_usd)}")
            if ev is None:
                c.rejected.append("no usable AtlasBench result for this model")
            else:
                if ev.quality < policy.min_quality:
                    c.rejected.append(f"quality {ev.quality:.2f} < required {policy.min_quality:.2f}")
                if ev.mean_call_latency_ms is None:
                    c.rejected.append("benchmark recorded no model calls, so latency is unknown")
                elif ev.mean_call_latency_ms > policy.max_latency_ms:
                    c.rejected.append(f"latency {ev.mean_call_latency_ms:.0f} ms/call > limit {policy.max_latency_ms} ms")
        c.eligible = not c.rejected
        cands.append(c)

    ok = sorted((c for c in cands if c.eligible),
                key=lambda c: (c.est_cost_usd, -c.evidence.quality, c.evidence.mean_call_latency_ms, c.model))
    if ok:
        pick = ok[0]
        cheaper_rejected = [f"{c.model} ({c.rejected[0]})" for c in cands if not c.eligible and c.est_cost_usd is not None and c.est_cost_usd < pick.est_cost_usd][:2]
        ev = pick.evidence
        reason = (f"Autopilot: cheapest of {len(ok)} qualifying model{'s' if len(ok) != 1 else ''} out of {len(cands)} — quality {ev.quality:.2f} ≥ {policy.min_quality:.2f}, "
                  f"{ev.mean_call_latency_ms:.0f} ms/call ≤ {policy.max_latency_ms} ms, est. {_usd(pick.est_cost_usd)} ≤ {_usd(policy.max_cost_per_call_usd)} "
                  f"(AtlasBench run {ev.run_id[:12]}…)." + (f" Cheaper but rejected: {'; '.join(cheaper_rejected)}." if cheaper_rejected else ""))
        return Decision("autopilot", pick.model, "autopilot_selected", reason, pick.est_cost_usd, ev.run_id, cands)

    fallback_cost = est_call_cost(policy.model, allowed, prompt_tokens, evidence.get(policy.model))
    if not any(c.evidence for c in cands):
        return Decision("autopilot", policy.model, "autopilot_no_evidence",
                        f"Autopilot could not choose: no usable AtlasBench results yet (run a benchmark on the candidate models). Using the default model {policy.model}.",
                        fallback_cost, None, cands)
    why = "; ".join(f"{c.model}: {c.rejected[0]}" for c in cands[:4])
    return Decision("autopilot", policy.model, "autopilot_no_qualifying_model",
                    f"Autopilot found no model meeting every requirement ({why}). Using the default model {policy.model}.", fallback_cost, None, cands)
