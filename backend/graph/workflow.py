from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from backend.graph.nodes import (
    agentic_execution_node,
    complete_research_node,
    credibility_node,
    evidence_extraction_node,
    executive_brief_node,
    finalize_report_node,
    initialize_research_node,
    plan_node,
    scrape_node,
    search_node,
    verification_node,
    writer_node,
)
from backend.graph.state import ResearchGraphState


def _after_agentic(state: ResearchGraphState) -> str:
    """Route successful controlled execution to the existing downstream report path.

    Any unexpected agentic failure uses the established fixed pipeline. This
    keeps the old path available as a real fallback rather than silently
    manufacturing an agentic result.
    """
    if state.get("agentic_status") in {"completed", "completed_with_gaps"}:
        return "writer"
    return "search"


def build_research_graph():
    graph = StateGraph(ResearchGraphState)

    graph.add_node("initialize", initialize_research_node)
    graph.add_node("plan", plan_node)
    graph.add_node("agentic_execution", agentic_execution_node)
    graph.add_node("search", search_node)
    graph.add_node("scrape", scrape_node)
    graph.add_node("evidence_extraction", evidence_extraction_node)
    graph.add_node("verification", verification_node)
    graph.add_node("writer", writer_node)
    graph.add_node("credibility", credibility_node)
    graph.add_node("executive_brief_step", executive_brief_node)
    graph.add_node("finalize_report", finalize_report_node)
    graph.add_node("complete", complete_research_node)

    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "plan")
    graph.add_edge("plan", "agentic_execution")
    graph.add_conditional_edges(
        "agentic_execution",
        _after_agentic,
        {"writer": "writer", "search": "search"},
    )

    # Existing fixed pipeline — retained as the safe fallback for any agentic
    # planning/execution failure or requests that intentionally disable
    # verification.
    graph.add_edge("search", "scrape")
    graph.add_edge("scrape", "evidence_extraction")
    graph.add_edge("evidence_extraction", "verification")
    graph.add_edge("verification", "writer")

    graph.add_edge("writer", "credibility")
    graph.add_edge("credibility", "executive_brief_step")
    graph.add_edge("executive_brief_step", "finalize_report")
    graph.add_edge("finalize_report", "complete")
    graph.add_edge("complete", END)

    return graph.compile()
