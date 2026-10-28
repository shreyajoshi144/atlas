"""Pure aggregation (one run) and comparison (several runs). No I/O.

Comparison is only meaningful over the same evidence, so runs are aligned on
``(task_id, task_hash)``: a task whose content changed between two runs is NOT
compared, and the response says so instead of quietly mixing versions.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from backend.bench.models import COMPOSITE_WEIGHTS, EVALUATOR_VERSION

METRIC_LABELS = {
    "claim_correctness": "Claim correctness", "citation_accuracy": "Citation accuracy",
    "evidence_grounding": "Evidence grounding", "evidence_coverage": "Evidence coverage",
    "contradiction_handling": "Contradiction handling", "trajectory_quality": "Tool / trajectory quality",
}
EPS = 1e-9


def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _percentile(values: list[int], pct: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))   # nearest-rank
    return ordered[min(rank, len(ordered)) - 1]


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """``cases``: [{status, scores, composite, flags, false_conflict_rate, usage, latency, tool_calls}]"""
    done = [c for c in cases if c["status"] == "completed"]
    metrics = {}
    for m in COMPOSITE_WEIGHTS:
        vals = [c["scores"][m] for c in done if c["scores"].get(m) is not None]
        metrics[m] = {"mean": _mean(vals), "n": len(vals)}
    comp = [c["composite"] for c in done if c["composite"] is not None]
    fcr = [c["false_conflict_rate"] for c in done if c.get("false_conflict_rate") is not None]
    latencies = [int(c["latency"].get("total_ms", 0)) for c in cases]
    costs = [c["usage"].get("cost_usd") for c in cases]
    known_costs = [x for x in costs if x is not None]
    toks = {k: [c["usage"].get(k) for c in cases if c["usage"].get(k) is not None] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    flags: dict[str, int] = {}
    for c in cases:
        for f in c.get("flags", []):
            flags[f] = flags.get(f, 0) + 1
    n = len(cases)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "cases_total": n, "cases_completed": len(done), "cases_failed": n - len(done),
        "completion_rate": round(len(done) / n, 4) if n else None,
        "metrics": metrics,
        "composite": {"mean": _mean(comp), "n": len(comp)},
        # failures counted as 0: a configuration that crashes on half the tasks must not look better for it
        "composite_incl_failures": round(sum(comp) / n, 4) if n and (comp or n) else None,
        "false_conflict_rate": {"mean": _mean(fcr), "n": len(fcr)},
        "performance": {
            "latency_ms_total": sum(latencies), "latency_ms_p50": _percentile(latencies, 50), "latency_ms_p95": _percentile(latencies, 95),
            "llm_latency_ms": sum(int(c["latency"].get("llm_ms", 0)) for c in cases),
            "tool_latency_ms": sum(int(c["latency"].get("tool_ms", 0)) for c in cases),
            "llm_calls": sum(int(c["usage"].get("llm_calls", 0)) for c in cases),
            "tool_calls": sum(int(c.get("tool_calls", 0)) for c in cases),
            "prompt_tokens": sum(toks["prompt_tokens"]) if toks["prompt_tokens"] else None,
            "completion_tokens": sum(toks["completion_tokens"]) if toks["completion_tokens"] else None,
            "total_tokens": sum(toks["total_tokens"]) if toks["total_tokens"] else None,
            "tokens_reported": bool(cases) and all(c["usage"].get("tokens_reported") for c in cases),
            "cost_usd": round(sum(known_costs), 6) if known_costs else None,
            "cost_complete": bool(cases) and len(known_costs) == n,   # False => cost covers only the cases that could be priced
        },
        "flags": dict(sorted(flags.items())),
    }


# ── comparison ───────────────────────────────────────────────────────────────
PERF_FIELDS = (("latency_ms_total", "Total latency (ms)"), ("latency_ms_p95", "p95 case latency (ms)"),
               ("total_tokens", "Total tokens"), ("cost_usd", "Estimated cost (USD)"), ("tool_calls", "Tool calls"))


def _best(values: dict[str, Optional[float]], higher: bool) -> Optional[str]:
    present = {k: v for k, v in values.items() if v is not None}
    if len(present) < 2:
        return None
    target = max(present.values()) if higher else min(present.values())
    winners = [k for k, v in present.items() if abs(v - target) < EPS]
    return winners[0] if len(winners) == 1 and len(set(round(v, 9) for v in present.values())) > 1 else None


def compare_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """``runs``: [{run_id, label, config_name, config, config_hash, fingerprint, evaluator_version, summary,
                  cases: {task_id: {task_hash, title, status, scores, composite, false_conflict_rate}}}]"""
    warnings: list[str] = []
    ids_ = [r["run_id"] for r in runs]
    first = runs[0]["cases"]
    common = sorted(t for t in first if all(t in r["cases"] and r["cases"][t]["task_hash"] == first[t]["task_hash"] for r in runs))
    all_tasks = {t for r in runs for t in r["cases"]}
    excluded = sorted(all_tasks - set(common))
    if excluded:
        warnings.append(f"{len(excluded)} task(s) were not part of every run, or changed between runs, and are excluded: {', '.join(excluded)}.")
    if len({r.get("evaluator_version") for r in runs}) > 1:
        warnings.append("Runs were scored by different evaluator versions; scores are not strictly comparable.")
    if not common:
        warnings.append("These runs share no identical tasks, so there is nothing to compare.")
    fingerprints = {r["fingerprint"] for r in runs}
    comparable = bool(common) and len({r.get("evaluator_version") for r in runs}) == 1

    def over_common(run: dict[str, Any], getter) -> Optional[float]:
        vals = [getter(run["cases"][t]) for t in common if run["cases"][t]["status"] == "completed"]
        return _mean([v for v in vals if v is not None])

    metrics = []
    for m, label in METRIC_LABELS.items():
        values = {r["run_id"]: over_common(r, lambda c, m=m: c["scores"].get(m)) for r in runs}
        base = values[ids_[0]]
        metrics.append({"metric": m, "label": label, "direction": "higher", "values": values, "best": _best(values, True),
                        "delta_vs_first": {k: (None if v is None or base is None else round(v - base, 4)) for k, v in values.items()}})
    comp = {r["run_id"]: over_common(r, lambda c: c["composite"]) for r in runs}
    comp_all = {}
    for r in runs:
        vals = [(r["cases"][t]["composite"] or 0.0) if r["cases"][t]["status"] == "completed" else 0.0 for t in common]
        comp_all[r["run_id"]] = round(sum(vals) / len(vals), 4) if vals else None
    composite = {"values": comp, "values_incl_failures": comp_all, "best": _best(comp_all, True),
                 "delta_vs_first": {k: (None if v is None or comp_all[ids_[0]] is None else round(v - comp_all[ids_[0]], 4)) for k, v in comp_all.items()}}

    performance = []
    for key, label in PERF_FIELDS:
        values = {r["run_id"]: r["summary"]["performance"].get(key) for r in runs}
        performance.append({"metric": key, "label": label, "direction": "lower", "values": values, "best": _best(values, False)})

    per_task, wins = [], {i: 0 for i in ids_}
    for t in common:
        vals = {r["run_id"]: (r["cases"][t]["composite"] if r["cases"][t]["status"] == "completed" else None) for r in runs}
        winner = _best(vals, True)
        if winner:
            wins[winner] += 1
        per_task.append({"task_id": t, "title": first[t].get("title", t), "composite": vals,
                         "status": {r["run_id"]: r["cases"][t]["status"] for r in runs}, "winner": winner})
    return {
        "comparable": comparable, "identical_setup": len(fingerprints) == 1, "warnings": warnings,
        "runs": [{"run_id": r["run_id"], "label": r["label"], "config_name": r["config_name"], "model": r["config"]["model"],
                  "strategy": r["config"]["strategy"], "config_hash": r["config_hash"], "fingerprint": r["fingerprint"],
                  "completion_rate": r["summary"].get("completion_rate")} for r in runs],
        "common_tasks": common, "excluded_tasks": excluded, "metrics": metrics, "composite": composite,
        "performance": performance, "per_task": per_task, "wins": wins,
    }


METRIC_INFO = {
    "claim_correctness": "Did each claim get the right verdict? Exact match scores 1, same polarity (e.g. verified vs partially verified) 0.5, a missed claim 0. Correct abstention when no evidence exists scores 1.",
    "citation_accuracy": "Are cited sources real, retrieved by this run, relevant to the claim, and on the right side (support vs contradict)?",
    "evidence_grounding": "Is every cited passage actually in the cited document, and is every affirmative verdict backed by at least one grounded supporting passage? Detects fabricated evidence.",
    "evidence_coverage": "Of the documents the gold labels as required for each claim, how many were cited for that claim?",
    "contradiction_handling": "For claims with contradicting evidence: was the conflict detected and were both sides surfaced? (Only tasks that contain a contradiction are scored.)",
    "trajectory_quality": "Tool use: required tools used, valid order, no duplicate calls, few errors, within budget, efficient relative to the evidence needed.",
}
