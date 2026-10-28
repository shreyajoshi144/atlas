from __future__ import annotations
from typing import Sequence
from backend.core.logging import get_logger
from backend.evaluation.evaluators import (
    evaluate_answer_groundedness, evaluate_claim_grounding, evaluate_retrieval,
    evaluate_verification_quality, evaluate_citation_correctness, evaluate_completeness,
    evaluate_agent_quality, evaluate_performance,
)
from backend.evaluation.models import EvaluationSummary, ResearchQualityEvaluation

logger = get_logger(__name__)

_RESEARCH_WEIGHTS = {
    "retrieval": 0.18,
    "grounding": 0.22,
    "citation_correctness": 0.15,
    "verification": 0.18,
    "completeness": 0.17,
    "answer_groundedness": 0.10,
}


def _weighted(values: dict[str, float | None], weights: dict[str, float]) -> float | None:
    present = {k: v for k, v in values.items() if v is not None}
    if not present:
        return None
    total = sum(weights[k] for k in present)
    return round(sum(float(present[k]) * weights[k] for k in present) / total, 4) if total else None


def run_pipeline_evaluation(
    *, topic: str, search_query: str | None, requested_top_k: int,
    ranked_sources: Sequence, evidence_text: str, verification_summary,
    verification_status: str, report_markdown: str, key_statistics: Sequence[str],
    report_id: str | None = None, session_id: str | None = None,
    research_plan=None, tool_trace=None, replan_count: int = 0,
    replan_history=None, agentic_trace=None, completed_action_ids=None,
    tool_call_count: int = 0, agentic_status: str | None = None,
    execution_metrics: dict | None = None, total_latency_ms: int | None = None,
    node_latency_ms: dict | None = None, registered_tools=None, token_usage: dict | None = None, estimated_cost_usd: float | None = None,
) -> EvaluationSummary:
    summary = EvaluationSummary(report_id=report_id, session_id=session_id, topic=topic)

    def run(name, fn, target):
        try:
            setattr(summary, target, fn())
        except Exception as exc:
            logger.warning(f"{name} evaluation failed (non-fatal)", extra={"error": str(exc)})
            summary.errors.append(f"{name}: {exc}")

    run("retrieval", lambda: evaluate_retrieval(search_query or topic, requested_top_k, list(ranked_sources or [])), "retrieval")
    run("grounding", lambda: evaluate_claim_grounding(verification_summary, evidence_text or ""), "grounding")
    run("verification_quality", lambda: evaluate_verification_quality(verification_summary, verification_status), "verification_quality")
    run("answer_groundedness", lambda: evaluate_answer_groundedness(report_markdown, list(key_statistics or []), list(ranked_sources or []), evidence_text or ""), "answer_groundedness")
    run("citation_correctness", lambda: evaluate_citation_correctness(report_markdown, list(ranked_sources or [])), "citation_correctness")
    run("completeness", lambda: evaluate_completeness(research_plan=research_plan, evidence_text=evidence_text or "", report_markdown=report_markdown or ""), "completeness")
    run("agent_quality", lambda: evaluate_agent_quality(
        research_plan=research_plan, tool_trace=list(tool_trace or []), replan_count=replan_count,
        replan_history=list(replan_history or []), agentic_trace=list(agentic_trace or []),
        completed_action_ids=list(completed_action_ids or []), tool_call_count=tool_call_count,
        agentic_status=agentic_status, registered_tools=registered_tools), "agent_quality")
    run("performance", lambda: evaluate_performance(
        total_latency_ms=total_latency_ms,
        tool_trace=list(tool_trace or []), tool_call_count=tool_call_count,
        replan_count=replan_count, token_usage=token_usage or {},
        estimated_cost_usd=estimated_cost_usd, node_latency_ms=node_latency_ms), "performance")

    summary.research_quality = ResearchQualityEvaluation(
        retrieval=summary.retrieval, grounding=summary.grounding,
        citation_correctness=summary.citation_correctness,
        verification=summary.verification_quality,
        completeness=summary.completeness,
        answer_groundedness=summary.answer_groundedness,
    )
    summary.research_quality.score = _weighted({
        "retrieval": summary.retrieval.score if summary.retrieval else None,
        "grounding": summary.grounding.score if summary.grounding else None,
        "citation_correctness": summary.citation_correctness.score if summary.citation_correctness else None,
        "verification": summary.verification_quality.score if summary.verification_quality else None,
        "completeness": summary.completeness.score if summary.completeness else None,
        "answer_groundedness": summary.answer_groundedness.score if summary.answer_groundedness else None,
    }, _RESEARCH_WEIGHTS)

    # Preserve the legacy overall field as a clearly documented research-quality
    # composite. Agent quality remains visible as independent metrics and is
    # deliberately not collapsed into this number.
    summary.overall_score = summary.research_quality.score

    components = [summary.retrieval, summary.grounding, summary.citation_correctness,
                  summary.verification_quality, summary.answer_groundedness,
                  summary.completeness, summary.agent_quality]
    for component in components:
        if component is not None:
            summary.flags.extend(component.flags)
    return summary
