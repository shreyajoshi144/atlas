"""Deterministic, evidence-first arbitration of existing AtlasBench outputs.

Arbitration consumes completed, independently measured cases over the same
frozen task. It never calls a model or invents a merged answer: it selects a
stored candidate using AtlasBench's existing evaluator scores.
"""
from __future__ import annotations

from typing import Any
import math

from backend.bench.models import COMPOSITE_WEIGHTS

# Selection requires at least three shared metrics spanning at least half of
# the documented composite weight. Below that threshold the evidence is too
# incomplete for a defensible winner.
MIN_COMPARABLE_METRICS = 3
MIN_COMPARABLE_WEIGHT = 0.50


class ArbitrationInputError(ValueError):
    pass


def arbitrate_candidates(candidates: list[dict[str, Any]], *, minimum_quality_score: float = 0.0) -> dict[str, Any]:
    if isinstance(minimum_quality_score, bool) or not isinstance(minimum_quality_score, (int, float)) or not math.isfinite(float(minimum_quality_score)) or not 0.0 <= float(minimum_quality_score) <= 1.0:
        raise ValueError("minimum_quality_score must be a finite number between 0 and 1.")
    minimum_quality_score = float(minimum_quality_score)
    eligible = [c for c in candidates if c.get("status") == "completed" and c.get("output") is not None]
    if len(eligible) < 2:
        raise ArbitrationInputError("Insufficient evidence: at least two completed outputs are required for comparison.")
    if len({c.get("model") for c in eligible}) < 2 or any(not c.get("model") for c in eligible):
        raise ArbitrationInputError("Eligible outputs must come from at least two distinct identified models.")
    hashes = {c.get("task_hash") for c in eligible}
    task_ids = [c.get("task_id") for c in eligible]
    evidence_hashes = [c.get("evidence_hash") for c in eligible]
    if (len(hashes) != 1 or None in hashes
            or (any(value is not None for value in task_ids) and (None in task_ids or len(set(task_ids)) != 1))
            or (any(value is not None for value in evidence_hashes)
                and (None in evidence_hashes or len(set(evidence_hashes)) != 1))):
        raise ArbitrationInputError("Eligible outputs must use the same frozen task and evidence hashes.")
    evaluators = {c.get("evaluator_version") for c in eligible}
    if len(evaluators) != 1 or None in evaluators:
        raise ArbitrationInputError("Eligible outputs must use the same recorded evaluator version.")
    # Compare on the same applicable metrics for every output. M8's composite
    # renormalizes its documented weights over measured metrics; here we also
    # require a shared measured set so differing N/A values cannot skew a race.
    def metric_value(candidate: dict[str, Any], key: str) -> float | None:
        value = (candidate.get("scores") or {}).get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None

    comparable = [key for key in COMPOSITE_WEIGHTS
                  if all(metric_value(candidate, key) is not None for candidate in eligible)]
    if not comparable:
        raise ArbitrationInputError("No meaningful AtlasBench score is shared by all completed candidates.")
    total_weight = sum(COMPOSITE_WEIGHTS[key] for key in comparable)
    if len(comparable) < MIN_COMPARABLE_METRICS or total_weight < MIN_COMPARABLE_WEIGHT:
        raise ArbitrationInputError(
            "Insufficient evidence: selection requires at least "
            f"{MIN_COMPARABLE_METRICS} shared metrics and {MIN_COMPARABLE_WEIGHT:.0%} of composite weight; "
            f"received {len(comparable)} metrics covering {total_weight:.0%}."
        )

    def weighted_score(candidate: dict[str, Any]) -> float:
        return sum(metric_value(candidate, key) * COMPOSITE_WEIGHTS[key] for key in comparable) / total_weight  # type: ignore[operator]

    # Selection and tie detection use the same precision persisted below. The
    # unrounded values remain internal and cannot break a tie hidden by the API.
    public_score = lambda candidate: round(weighted_score(candidate), 4)
    ranked = sorted(eligible, key=lambda c: (-public_score(c), str(c.get("model", "")), str(c.get("run_id", ""))))
    if public_score(ranked[0]) == public_score(ranked[1]):
        raise ArbitrationInputError("Insufficient evidence: the top candidates have indistinguishable comparable composite scores.")
    chosen = ranked[0]
    chosen_score = public_score(chosen)
    if chosen_score < minimum_quality_score:
        reason = (
            "The best measured composite score "
            f"({chosen_score:.4f}) is below the configured minimum quality threshold ({minimum_quality_score:.4f}); no candidate was selected."
        )
        return {
            "task_hash": next(iter(hashes)),
            "decision": "abstained",
            "selected_run_id": None,
            "selected_model": None,
            "selected_output": None,
            "comparable_metrics": comparable,
            "candidate_composite_scores": {
                str(candidate.get("run_id")): round(weighted_score(candidate), 4) for candidate in eligible
            },
            "rationale": [
                f"Configured minimum measured composite quality threshold: {minimum_quality_score:.4f}.",
                "All candidates were evaluated against the same frozen evidence snapshot.",
                f"Measured composite scores were calculated over shared applicable metrics: {', '.join(comparable)}.",
                reason,
                "No model agreement or heuristic confidence was treated as proof of correctness.",
            ],
            "abstention_reason": reason,
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "evaluator_version": next(iter(evaluators)),
            "minimum_quality_score": minimum_quality_score,
            "quality_score_kind": "measured_composite_of_evaluator_metrics",
        }
    rationale = [
        f"Configured minimum measured composite quality threshold: {minimum_quality_score:.4f}.",
        "All candidates were evaluated against the same frozen evidence snapshot.",
        f"Selection used the documented weighted AtlasBench composite over shared applicable metrics: {', '.join(comparable)}.",
        f"Selected {chosen.get('model')} from run {chosen.get('run_id')} with comparable composite score {weighted_score(chosen):.4f}.",
        "The selected stored output is returned unchanged; no unsupported synthesis was generated.",
    ]
    failures = len(candidates) - len(eligible)
    if failures:
        rationale.append(f"Excluded {failures} candidate result(s) that failed or lacked a completed evaluator output; their status and provider traces remain in this record.")
    return {
        "task_hash": next(iter(hashes)),
        "decision": "selected",
        "selected_run_id": chosen.get("run_id"),
        "selected_model": chosen.get("model"),
        "selected_output": chosen["output"],
        "comparable_metrics": comparable,
        "candidate_composite_scores": {
            str(candidate.get("run_id")): round(weighted_score(candidate), 4) for candidate in eligible
        },
        "rationale": rationale,
        "candidate_count": len(candidates),
        "eligible_candidate_count": len(eligible),
        "evaluator_version": next(iter(evaluators)),
        "minimum_quality_score": minimum_quality_score,
        "quality_score_kind": "measured_composite_of_evaluator_metrics",
    }
