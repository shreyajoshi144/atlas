from __future__ import annotations

from typing import Any, Optional, TypedDict

from backend.agentic.planning.models import ResearchPlan
from backend.models.common import MetricBreakdown, ResearchStatus
from backend.models.domain_models import (
    ExecutiveBrief,
    RankedSource,
    ResearchReport,
    ResearchSession,
    ScrapedContent,
    SearchResult,
    VerificationSummary,
)

class ResearchGraphState(TypedDict, total=False):
    topic: str
    research_mode: str
    user_context: Optional[str]
    authenticated_user_id: str

    # NOTE: these two were previously missing from this TypedDict schema.
    # LangGraph's StateGraph(ResearchGraphState) filters graph.invoke()'s
    # input dict down to only the keys declared here, silently dropping
    # anything else *before* any node runs. Because top_k_urls and
    # search_query were absent, the API's requested_top_k (e.g. 12, or the
    # mode default of 8/12/14/20) never reached search_node — it always saw
    # a missing key and fell back to state.get("top_k_urls", 5), which is
    # the exact "requested 12, Tavily got 5" bug. Declaring the fields here
    # is the actual fix; search_node's fallback default is intentionally
    # left as a last-resort safety net, not the source of truth.
    top_k_urls: int
    search_query: str

    # Agentic execution state (Phase 1C/1D). The legacy graph remains the
    # safe fallback; these fields are populated only by an explicit agentic
    # execution caller.
    research_plan: ResearchPlan
    current_action_id: Optional[str]
    completed_action_ids: list[str]
    tool_results: dict[str, Any]
    action_tool_results: dict[str, list[Any]]
    tool_trace: list[dict[str, Any]]
    tool_call_count: int
    agentic_status: str
    agentic_error: Optional[str]
    execution_metrics: dict[str, Any]
    evidence_gaps: list[Any]
    replan_count: int
    replan_history: list[dict[str, Any]]
    newly_discovered_sources: list[Any]
    agentic_trace: list[dict[str, Any]]
    research_trace: list[dict[str, Any]]

    session: ResearchSession
    status: ResearchStatus
    metrics: MetricBreakdown

    search_results: list[SearchResult]
    ranked_sources: list[RankedSource]
    scraped_contents: list[ScrapedContent]

    evidence_text: str
    verification_summary: VerificationSummary
    verification_status: str
    agentic_verification_status: str

    generated_summary: str
    generated_report_markdown: str
    key_statistics: list[str]
    executive_brief: ExecutiveBrief
    final_report: ResearchReport

    include_verification: bool
    include_executive_brief: bool
    persist_memory: bool

    errors: list[str]
    debug: dict[str, Any]