"""AtlasBench results as routing evidence.

For each model, the user's most recent *usable* completed run: same scorer version as the current
code, fresh enough, large enough, and (optionally) of the requested strategy. Quality is the
conservative ``composite_incl_failures`` (a task that crashed counts as 0), so a model that
fails often cannot look good. Latency and token averages are per model CALL, the unit a routing
decision is made for. Nothing is invented: a missing figure stays ``None`` and the selector
treats it as "cannot verify", never as "fine".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.bench.models import EVALUATOR_VERSION
from backend.bench.repository import BenchRepository
from backend.routing.models import RoutingPolicy


@dataclass(frozen=True)
class ModelEvidence:
    model: str
    run_id: str
    completed_at: str
    strategy: str
    quality: float
    cases: int
    completion_rate: Optional[float]
    mean_call_latency_ms: Optional[float]
    avg_prompt_tokens: Optional[float]
    avg_completion_tokens: Optional[float]

    def public(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "completed_at": self.completed_at, "strategy": self.strategy, "quality": self.quality, "cases": self.cases,
                "completion_rate": self.completion_rate, "mean_call_latency_ms": self.mean_call_latency_ms,
                "avg_prompt_tokens": self.avg_prompt_tokens, "avg_completion_tokens": self.avg_completion_tokens}


def _f(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return dt if dt is None or dt.tzinfo else (dt.replace(tzinfo=timezone.utc) if dt else None)


def evidence_from_run(row: dict[str, Any]) -> Optional[ModelEvidence]:
    try:
        summary = json.loads(row["summary_json"]) if row.get("summary_json") else None
        config = json.loads(row["config_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(summary, dict) or not isinstance(config, dict) or not config.get("model"):
        return None
    quality = _f(summary.get("composite_incl_failures"))
    if quality is None:
        return None
    perf = summary.get("performance") or {}
    calls = _f(perf.get("llm_calls")) or 0.0
    latency = (_f(perf.get("llm_latency_ms")) / calls) if calls and _f(perf.get("llm_latency_ms")) is not None else None
    reported = bool(perf.get("tokens_reported"))
    p, c = _f(perf.get("prompt_tokens")), _f(perf.get("completion_tokens"))
    return ModelEvidence(
        model=config["model"], run_id=row["run_id"], completed_at=row.get("completed_at") or row.get("created_at") or "", strategy=config.get("strategy", "any"),
        quality=quality, cases=int(summary.get("cases_total") or 0), completion_rate=_f(summary.get("completion_rate")), mean_call_latency_ms=latency,
        avg_prompt_tokens=(p / calls) if reported and calls and p is not None else None,
        avg_completion_tokens=(c / calls) if reported and calls and c is not None else None)


def load_evidence(bench: BenchRepository, user_id: str, policy: RoutingPolicy, now: datetime) -> dict[str, ModelEvidence]:
    """{model: newest usable evidence} from THIS user's completed runs only."""
    rows, _ = bench.list_runs(user_id, status="completed", limit=100)
    cutoff = now - timedelta(days=policy.max_evidence_age_days)
    best: dict[str, ModelEvidence] = {}
    for row in rows:                                    # newest first
        if row.get("evaluator_version") != EVALUATOR_VERSION:
            continue
        ev = evidence_from_run(row)
        if ev is None or ev.model in best:
            continue
        done = _parse_ts(ev.completed_at)
        if done is None or done < cutoff or ev.cases < policy.min_evidence_cases:
            continue
        if policy.evidence_strategy != "any" and ev.strategy != policy.evidence_strategy:
            continue
        best[ev.model] = ev
    return best
