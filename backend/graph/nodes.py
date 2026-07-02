from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import datetime
from functools import lru_cache

from backend.core.logging import get_logger
from backend.models.common import MetricBreakdown, ResearchStatus
from backend.models.domain_models import ResearchReport, ResearchSession

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


def initialize_research_node(state):
    topic = state["topic"]
    session = ResearchSession(topic=topic, status=ResearchStatus.RUNNING, query_text=topic)
    metrics = MetricBreakdown()
    logger.info("Initialized research session", extra={"topic": topic, "session_id": session.session_id})
    return {**state, "session": session, "status": ResearchStatus.RUNNING, "metrics": metrics, "errors": [], "debug": {"started_at": time.perf_counter()}}


def search_node(state):
    start = time.perf_counter()
    topic = state["topic"]
    top_k = state.get("top_k_urls", 5)
    search_results, ranked_sources = _retrieval_service().retrieve_and_rank(query=topic, top_k=top_k)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.retrieval_latency_ms = elapsed_ms
    logger.info("Search node completed", extra={"topic": topic, "ranked_source_count": len(ranked_sources), "latency_ms": elapsed_ms})
    return {**state, "search_results": search_results, "ranked_sources": ranked_sources, "metrics": metrics}


def scrape_node(state):
    start = time.perf_counter()
    ranked_sources = state.get("ranked_sources", [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _scraping_service().scrape_sources(ranked_sources))
        scraped_contents = future.result()
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.scraping_latency_ms = elapsed_ms
    logger.info("Scrape node completed", extra={"scraped_count": len(scraped_contents), "latency_ms": elapsed_ms})
    return {**state, "scraped_contents": scraped_contents, "metrics": metrics}


def evidence_extraction_node(state):
    evidence_text = _content_merge_service().merge(
        ranked_sources=state.get("ranked_sources", []),
        scraped_contents=state.get("scraped_contents", []),
    )
    logger.info("Evidence extraction node completed", extra={"evidence_length": len(evidence_text)})
    return {**state, "evidence_text": evidence_text}


def verification_node(state):
    if not state.get("include_verification", True):
        return {**state, "verification_summary": None}
    verification_summary = _verification_agent().verify(
        report_markdown="",
        scraped_research_text=state.get("evidence_text", ""),
    )
    logger.info("Verification node completed", extra={"verification_score": verification_summary.overall_verification_score})
    return {**state, "verification_summary": verification_summary}


def writer_node(state):
    start = time.perf_counter()
    topic = state["topic"]
    evidence_text = state.get("evidence_text", "")
    verification_summary = state.get("verification_summary")
    verification_block = ""
    if verification_summary:
        verification_block = (
            f"\n\nVerification Verdict: {verification_summary.verdict}\n"
            f"Overall Verification Score: {verification_summary.overall_verification_score}\n"
        )
    writer_output = _writer_agent().generate_report(topic=topic, research_text=f"{evidence_text}{verification_block}")
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    metrics = state["metrics"]
    metrics.llm_latency_ms += elapsed_ms
    logger.info("Writer node completed", extra={"topic": topic, "latency_ms": elapsed_ms})
    return {**state, "generated_summary": writer_output.summary, "generated_report_markdown": writer_output.report_markdown, "key_statistics": writer_output.key_statistics, "metrics": metrics}


def credibility_node(state):
    ranked_sources = _credibility_service().score_sources(state.get("ranked_sources", []))
    logger.info("Credibility node completed", extra={"source_count": len(ranked_sources)})
    return {**state, "ranked_sources": ranked_sources}


def executive_brief_node(state):
    if not state.get("include_executive_brief", True):
        return state
    verification_summary = state.get("verification_summary")
    verification_score = verification_summary.overall_verification_score if verification_summary else None
    executive_brief = _executive_brief_agent().generate(
        topic=state["topic"],
        report_markdown=state.get("generated_report_markdown", ""),
        verification_score=verification_score,
    )
    logger.info("Executive brief node completed", extra={"topic": state["topic"]})
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
    return {**state, "final_report": final_report}


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
    return {**state, "session": session, "status": ResearchStatus.COMPLETED, "metrics": metrics}
