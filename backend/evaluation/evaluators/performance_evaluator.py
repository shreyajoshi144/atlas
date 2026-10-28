from __future__ import annotations
from backend.evaluation.models import SystemPerformanceEvaluation


def evaluate_performance(*, total_latency_ms: int | None, tool_trace, tool_call_count: int | None,
                         replan_count: int | None, token_usage: dict | None = None,
                         estimated_cost_usd: float | None = None, node_latency_ms: dict | None = None) -> SystemPerformanceEvaluation:
    token_usage = token_usage or {}
    prompt = token_usage.get("prompt_tokens")
    completion = token_usage.get("completion_tokens")
    total = token_usage.get("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    # Zero is treated as unavailable because existing providers use 0 as the
    # default when they do not expose usage; evaluation must never invent cost.
    total = int(total) if total is not None and total > 0 else None
    prompt = int(prompt) if prompt is not None and prompt > 0 else None
    completion = int(completion) if completion is not None and completion > 0 else None
    cost = float(estimated_cost_usd) if estimated_cost_usd is not None and estimated_cost_usd > 0 else None
    return SystemPerformanceEvaluation(
        total_latency_ms=total_latency_ms,
        tool_call_count=tool_call_count,
        replan_count=replan_count,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        estimated_cost_usd=cost,
        node_latency_ms=dict(node_latency_ms or {}),
        per_tool_latency_ms=[{"action_id": r.get("action_id"), "tool": r.get("tool"), "duration_ms": r.get("duration_ms", 0)} for r in (tool_trace or [])],
    )
