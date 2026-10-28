from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import datetime
from functools import lru_cache

from backend.core.logging import get_logger
from backend.core.observability import append_trace, classify_error
from backend.models.common import MetricBreakdown, ResearchStatus
from backend.models.domain_models import (
    RankedSource,
    ResearchReport,
    ResearchSession,
    SearchResult,
    ScrapedContent,
    VerificationSummary,
)

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _retrieval_service():
    from backend.services.retrieval_service import RetrievalService
    return RetrievalService()

@lru_cache(maxsize=1)
def _scraping_service():
    from backend.services.scraping_service import AsyncScrapingService
    return AsyncScrapingService()

@lru_cache(maxsize=1)
def _content_merge_service():
    from backend.services.content_merge_service import ContentMergeService
    return ContentMergeService()

@lru_cache(maxsize=1)
def _verification_agent():
    from backend.agents.verification_agent import VerificationAgent
    return VerificationAgent()

@lru_cache(maxsize=1)
def _writer_agent():
    from backend.agents.writer_agent import WriterAgent
    return WriterAgent()

@lru_cache(maxsize=1)
def _credibility_service():
    from backend.services.credibility_service import CredibilityScoringService
    return CredibilityScoringService()

@lru_cache(maxsize=1)
def _executive_brief_agent():
    from backend.agents.executive_brief_agent import ExecutiveBriefAgent
    return ExecutiveBriefAgent()

@lru_cache(maxsize=1)
def _research_planner():
    from backend.agentic.planning import ResearchPlanner
    return ResearchPlanner()


@lru_cache(maxsize=1)
def _agentic_executor():
    from backend.agentic.execution import AgenticToolExecutor
    from backend.agentic.tools import get_tool_registry
    return AgenticToolExecutor(get_tool_registry())


def _materialize_agentic_result(state, result):
    """Translate the controlled executor's typed results into the legacy graph state.

    The writer/evaluation/persistence layers continue consuming their established
    state contracts; Phase 1 integration changes only how evidence and verification
    are produced.
    """
    from backend.agentic.common import ToolName
    from backend.agentic.tools.schemas import (
        RequestMoreEvidenceOutput,
        SearchWebOutput,
        ScrapeSourceOutput,
        VerifyClaimOutput,
    )

    sources: list[RankedSource] = []
    scraped: list[ScrapedContent] = []
    seen_urls: set[str] = set()
    latest_verification = None
    search_results: list[SearchResult] = []
    retrieval_ms = 0
    scraping_ms = 0
    verification_ms = 0

    for action_id, results in result.get("action_tool_results", {}).items():
        for tool_result in results:
            tool = tool_result.tool
            if tool in {ToolName.SEARCH_WEB.value, ToolName.REQUEST_MORE_EVIDENCE.value}:
                retrieval_ms += int(tool_result.duration_ms or 0)
            elif tool == ToolName.SCRAPE_SOURCE.value:
                scraping_ms += int(tool_result.duration_ms or 0)
            elif tool == ToolName.VERIFY_CLAIM.value:
                verification_ms += int(tool_result.duration_ms or 0)

            if not tool_result.success or tool_result.output is None:
                continue
            output = tool_result.output
            if isinstance(output, SearchWebOutput):
                candidates = output.sources
                for source in candidates:
                    key = str(source.url).rstrip("/").lower()
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    sources.append(source)
                    search_results.append(SearchResult(
                        title=source.title,
                        url=source.url,
                        snippet=source.snippet,
                        domain=source.domain,
                        rank_position=source.rank_position,
                        published_date=source.publish_date,
                        source_type=source.source_type,
                    ))
            elif isinstance(output, RequestMoreEvidenceOutput):
                for source in output.new_sources:
                    key = str(source.url).rstrip("/").lower()
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    sources.append(source)
                    search_results.append(SearchResult(
                        title=source.title,
                        url=source.url,
                        snippet=source.snippet,
                        domain=source.domain,
                        rank_position=source.rank_position,
                        published_date=source.publish_date,
                        source_type=source.source_type,
                    ))
            elif isinstance(output, ScrapeSourceOutput):
                if output.scraped and output.success:
                    scraped.append(output.scraped)
            elif isinstance(output, VerifyClaimOutput):
                latest_verification = output

    evidence_text = _content_merge_service().merge(
        ranked_sources=sources,
        scraped_contents=scraped,
    )

    verification_summary = latest_verification.verification if latest_verification else None
    if verification_summary is not None:
        source_by_url = {str(source.url).rstrip("/").lower(): source for source in sources}
        for claim in verification_summary.claims:
            for evidence in claim.evidence:
                source = source_by_url.get(str(evidence.url).rstrip("/").lower())
                if source is not None:
                    evidence.source_id = source.source_id
                    evidence.publish_date = source.publish_date
    agentic_verification_status = result.get("verification_status", "not_run")
    verification_status = "succeeded" if verification_summary is not None else "unavailable"

    metrics = state["metrics"]
    metrics.retrieval_latency_ms = retrieval_ms
    metrics.scraping_latency_ms = scraping_ms
    metrics.llm_latency_ms = verification_ms

    return {
        **state,
        **result,
        "search_results": search_results,
        "ranked_sources": sources,
        "scraped_contents": scraped,
        "evidence_text": evidence_text,
        "verification_summary": verification_summary,
        "verification_status": verification_status,
        "agentic_verification_status": agentic_verification_status,
        "metrics": metrics,
    }


def initialize_research_node(state):
    topic = state["topic"]
    session = ResearchSession(topic=topic, status=ResearchStatus.RUNNING, query_text=topic)
    metrics = MetricBreakdown()
    logger.info("Initialized research session", extra={"topic": topic, "session_id": session.session_id})
    return {**state, "session": session, "status": ResearchStatus.RUNNING, "metrics": metrics, "errors": [], "research_trace": append_trace({**state, "session": session}, component="research", action="request", status="started", metadata={"research_mode": state.get("research_mode", "explore")}), "debug": {"started_at": time.perf_counter()}}


def plan_node(state):
    """Phase 1A: attach a validated ResearchPlan to state.

    Purely advisory for now — no tool is executed and no later node reads the
    plan, so the pipeline behaves exactly as before. Planning is deterministic
    and local, but any failure is still contained so it can never break a run.
    """
    try:
        plan = _research_planner().create_plan(
            question=state["topic"],
            research_mode=state.get("research_mode", "explore"),
            top_k_urls=state.get("top_k_urls"),
        )
    except Exception as exc:
        logger.warning("Research planning failed — continuing without a plan", extra={"error": str(exc)})
        return {**state, "research_trace": append_trace(state, component="planner", action="create_plan", status="failed", error=exc)}
    logger.info(
        "Research plan created",
        extra={
            "plan_id": plan.plan_id,
            "research_mode": plan.research_mode,
            "sub_question_count": len(plan.sub_questions),
            "planned_action_count": len(plan.planned_actions),
        },
    )
    return {**state, "research_plan": plan, "research_trace": append_trace(state, component="planner", action="create_plan", status="succeeded", metadata={"planned_action_count": len(plan.planned_actions), "sub_question_count": len(plan.sub_questions)})}


def agentic_execution_node(state):
    """Execute the validated plan through the Phase 1C–1E controlled loop.

    This is the integration boundary for normal research requests. If the
    controlled path cannot complete safely, the graph routes to the existing
    fixed search→scrape→evidence→verification pipeline rather than fabricating
    a result.
    """
    if not state.get("include_verification", True):
        return {
            **state,
            "agentic_status": "skipped",
            "agentic_error": "agentic execution requires verification; using legacy fallback",
            "research_trace": append_trace(state, component="agentic_execution", action="controlled_execution", status="skipped"),
        }
    plan = state.get("research_plan")
    if plan is None:
        return {**state, "agentic_status": "failed", "agentic_error": "validated research plan is missing", "research_trace": append_trace(state, component="agentic_execution", action="controlled_execution", status="failed", error="validated research plan is missing")}
    try:
        result = _agentic_executor().run(
            plan,
            execution_context={"user_id": state.get("authenticated_user_id")},
            initial_state=state,
        )
        if result.get("agentic_status") not in {"completed", "completed_with_gaps"}:
            merged = {**state, **result, "agentic_status": result.get("agentic_status", "failed")}
            return {**merged, "research_trace": append_trace(merged, component="agentic_execution", action="controlled_execution", status=result.get("agentic_status", "failed"), metadata={"tool_call_count": result.get("tool_call_count", 0), "replan_count": result.get("replan_count", 0)})}
        materialized = _materialize_agentic_result(state, result)
        return {**materialized, "research_trace": append_trace(materialized, component="agentic_execution", action="controlled_execution", status="succeeded", metadata={"tool_call_count": result.get("tool_call_count", 0), "replan_count": result.get("replan_count", 0)})}
    except Exception as exc:  # noqa: BLE001 - integration boundary must fail safely
        logger.exception("Agentic research execution failed; routing to legacy fallback", extra={"error": str(exc)})
        return {
            **state,
            "agentic_status": "failed",
            "agentic_error": f"controlled agentic execution failed: {type(exc).__name__}",
            "research_trace": append_trace(state, component="agentic_execution", action="controlled_execution", status="failed", error=exc),
        }


def search_node(state):
    start = time.perf_counter()
    topic = state["topic"]
    top_k = state.get("top_k_urls", 5)
    search_query = state.get("search_query") or topic
    search_results, ranked_sources = _retrieval_service().retrieve_and_rank(query=search_query, top_k=top_k, research_mode=state.get("research_mode", "explore"))
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.retrieval_latency_ms = elapsed_ms
    logger.info("Search node completed", extra={"topic": topic, "research_mode": state.get("research_mode", "explore"), "requested_top_k": top_k, "ranked_source_count": len(ranked_sources), "latency_ms": elapsed_ms})
    return {**state, "search_results": search_results, "ranked_sources": ranked_sources, "metrics": metrics, "research_trace": append_trace(state, component="retrieval", action="search", status="succeeded", duration_ms=elapsed_ms, metadata={"source_count": len(ranked_sources)})}


def scrape_node(state):
    start = time.perf_counter()
    ranked_sources = state.get("ranked_sources", [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _scraping_service().scrape_sources(ranked_sources))
        scraped_contents = future.result()

    # Preserve the existing source objects used by the rest of the graph.
    # Tavily's publication date is authoritative; only fill a missing date
    # from the scraper's conservative structured-metadata extraction.
    scraped_by_source_id = {
        content.source_id: content
        for content in scraped_contents
        if content.publish_date is not None
    }
    for source in ranked_sources:
        if source.publish_date is None:
            scraped = scraped_by_source_id.get(source.source_id)
            if scraped is not None:
                source.publish_date = scraped.publish_date

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.scraping_latency_ms = elapsed_ms
    logger.info("Scrape node completed", extra={"scraped_count": len(scraped_contents), "latency_ms": elapsed_ms})
    return {**state, "scraped_contents": scraped_contents, "metrics": metrics, "research_trace": append_trace(state, component="scraper", action="scrape", status="succeeded", duration_ms=elapsed_ms, metadata={"scraped_count": len(scraped_contents)})}


def evidence_extraction_node(state):
    evidence_text = _content_merge_service().merge(
        ranked_sources=state.get("ranked_sources", []),
        scraped_contents=state.get("scraped_contents", []),
    )
    logger.info("Evidence extraction node completed", extra={"evidence_length": len(evidence_text)})
    return {**state, "evidence_text": evidence_text, "research_trace": append_trace(state, component="evidence", action="extract", status="succeeded", metadata={"evidence_chars": len(evidence_text), "source_count": len(state.get("ranked_sources", []))})}


def verification_node(state):
    if not state.get("include_verification", True):
        return {**state, "verification_summary": None, "verification_status": "unavailable"}
    try:
        verification_summary = _verification_agent().verify(
            report_markdown="",
            scraped_research_text=state.get("evidence_text", ""),
        )
        # Attach the real source_id to each evidence item by URL. This keeps
        # downstream contradiction/conclusion counts tied to actual sources.
        source_by_url = {str(s.url).rstrip("/"): s for s in state.get("ranked_sources", [])}
        for claim in verification_summary.claims:
            for evidence in claim.evidence:
                source = source_by_url.get(str(evidence.url).rstrip("/"))
                if source:
                    evidence.source_id = source.source_id
                    evidence.publish_date = source.publish_date
        logger.info("Verification node completed", extra={"verification_score": verification_summary.overall_verification_score})
    except Exception as exc:
        logger.error("Verification node failed — continuing without verification", extra={"error": str(exc)})
        # Keep the existing typed verification representation in graph state.
        # An empty summary is explicitly unverified; it is never treated as a
        # successful verification result and contains no fabricated claims.
        verification_summary = VerificationSummary(verdict="unverified")
        return {**state, "verification_summary": verification_summary, "verification_status": "failed", "research_trace": append_trace(state, component="verification", action="verify", status="failed", error=exc)}
    return {**state, "verification_summary": verification_summary, "verification_status": "succeeded", "research_trace": append_trace(state, component="verification", action="verify", status="succeeded", metadata={"claim_count": len(verification_summary.claims), "verification_score": verification_summary.overall_verification_score})}


def writer_node(state):
    start = time.perf_counter()
    topic = state["topic"]
    evidence_text = state.get("evidence_text", "")
    mode = state.get("research_mode", "explore")
    verification_summary = state.get("verification_summary")
    verification_block = ""
    if verification_summary:
        verification_block = (
            f"\n\nVerification Verdict: {verification_summary.verdict}\n"
            f"Overall Verification Score: {verification_summary.overall_verification_score}\n"
        )
    mode_guidance = {
        "explore": "Build a broad evidence map and identify the major themes and perspectives.",
        "compare": "Explicitly compare the alternatives, dimensions, trade-offs, and evidence differences present in the research.",
        "investigate": "Prioritize focused causal evidence, mechanisms, uncertainties, and unresolved claims.",
        "literature_review": "Synthesize themes across the literature, distinguish areas of agreement/disagreement, and note evidence gaps.",
        "brief": "Produce a concise decision-oriented brief while retaining evidence limitations and source grounding.",
    }.get(mode, "Build a broad evidence map.")
    writer_output = _writer_agent().generate_report(topic=topic, research_text=f"Research mode: {mode}. {mode_guidance}\n\n{evidence_text}{verification_block}")
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.llm_latency_ms += elapsed_ms
    logger.info("Writer node completed", extra={"topic": topic, "latency_ms": elapsed_ms})
    return {**state, "generated_summary": writer_output.summary, "generated_report_markdown": writer_output.report_markdown, "key_statistics": writer_output.key_statistics, "metrics": metrics, "research_trace": append_trace(state, component="writer", action="generate_report", status="succeeded", duration_ms=elapsed_ms, metadata={"report_length": len(writer_output.report_markdown)})}


def credibility_node(state):
    ranked_sources = _credibility_service().score_sources(state.get("ranked_sources", []))
    logger.info("Credibility node completed", extra={"source_count": len(ranked_sources)})
    return {**state, "ranked_sources": ranked_sources}


def executive_brief_node(state):
    if not state.get("include_executive_brief", True):
        return state
    verification_summary = state.get("verification_summary")
    verification_score = verification_summary.overall_verification_score if verification_summary else None
    try:
        executive_brief = _executive_brief_agent().generate(
            topic=state["topic"],
            report_markdown=state.get("generated_report_markdown", ""),
            verification_score=verification_score,
        )
        logger.info("Executive brief node completed", extra={"topic": state["topic"]})
    except Exception as exc:
        logger.error("Executive brief node failed — continuing without it", extra={"error": str(exc)})
        executive_brief = None
    return {**state, "executive_brief": executive_brief}


def finalize_report_node(state):
    final_report = ResearchReport(
        topic=state["topic"],
        summary=state.get("generated_summary", ""),
        report_markdown=state.get("generated_report_markdown", ""),
        key_statistics=state.get("key_statistics", []),
        sources=state.get("ranked_sources", []),
        verification=state.get("verification_summary"),
        executive_brief=state.get("executive_brief"),
    )
    logger.info("Finalized report", extra={"topic": state["topic"], "report_id": final_report.report_id})
    return {**state, "final_report": final_report, "research_trace": append_trace(state, component="finalization", action="finalize_report", status="succeeded", metadata={"report_id": final_report.report_id})}


def complete_research_node(state):
    start_time = state.get("debug", {}).get("started_at")
    total_execution_ms = int((time.perf_counter() - start_time) * 1000) if start_time else 0
    metrics = state["metrics"]
    metrics.total_execution_ms = total_execution_ms
    session = state["session"]
    session.status = ResearchStatus.COMPLETED
    session.report_id = state["final_report"].report_id
    session.updated_at = datetime.utcnow()
    session.metrics = metrics
    logger.info("Research workflow completed", extra={"session_id": session.session_id, "total_execution_ms": total_execution_ms})
    return {**state, "session": session, "status": ResearchStatus.COMPLETED, "metrics": metrics, "research_trace": append_trace(state, component="research", action="complete", status="succeeded", duration_ms=total_execution_ms, metadata={"report_id": state["final_report"].report_id})}
