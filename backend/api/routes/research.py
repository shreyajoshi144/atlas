from __future__ import annotations

import asyncio
import time
import traceback
from functools import lru_cache

from fastapi import APIRouter, HTTPException

from backend.graph.workflow import build_research_graph
from backend.models.api_models import ResearchRunRequest
from backend.models.response_models import (
    ApiResponse, ErrorResponse, ExecutiveBriefResponse,
    ResearchRunResponse, ResearchReportResponse, ResearchSessionResponse,
    SourceResponse, VerificationSummaryResponse, VerifiedClaimResponse,
)
from backend.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    422: {"description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@lru_cache(maxsize=1)
def _repository():
    from backend.database import ResearchRepository
    repo = ResearchRepository()
    repo.initialize()
    return repo


@lru_cache(maxsize=1)
def _vector_memory():
    from backend.memory.retrieval_service import VectorMemoryService
    return VectorMemoryService()


def _normalise_verification_score(score: float) -> float:
    """Domain model stores verification score as 0–100. API/frontend expects 0–1."""
    if score > 1.0:
        return round(score / 100.0, 4)
    return round(score, 4)


@router.post("/run", response_model=ApiResponse[ResearchRunResponse], responses=ERROR_RESPONSES)
async def run_research(request: ResearchRunRequest) -> ApiResponse[ResearchRunResponse]:
    try:
        graph = build_research_graph()
        initial_state = {
            "top_k_urls": request.top_k_urls,
            "topic": request.topic,
            "user_context": request.user_context,
            "include_verification": request.include_verification,
            "include_executive_brief": request.include_executive_brief,
            "persist_memory": request.persist_memory,
            "debug": {"started_at": time.perf_counter()},
            "errors": [],
        }

        loop = asyncio.get_event_loop()
        result_state = await loop.run_in_executor(None, graph.invoke, initial_state)

        session = result_state["session"]
        report = result_state["final_report"]
        ranked_sources = result_state.get("ranked_sources", [])
        verification = result_state.get("verification_summary")

        # ── Persist to SQLite + ChromaDB ─────────────────────────────────────
        repository = _repository()
        if request.persist_memory:
            try:
                topic_id = repository.topics.get_or_create_topic(request.topic)
                # save_session expects (topic_id, session)
                repository.sessions.save_session(topic_id, session)
                # save_report expects (topic_id, session, report)
                repository.reports.save_report(topic_id, session, report)
                for src in ranked_sources:
                    repository.sources.save_source(report.report_id, src)
                try:
                    _vector_memory().index_report(topic_id=topic_id, session=session, report=report)
                except Exception as ve:
                    logger.warning("Vector indexing failed (non-fatal)", extra={"error": str(ve)})
            except Exception as db_exc:
                logger.error("DB persistence failed (non-fatal)", extra={"error": str(db_exc), "tb": traceback.format_exc()})
                # Non-fatal: still return the result to the user

        # ── Build response ────────────────────────────────────────────────────
        metrics = session.metrics
        session_resp = ResearchSessionResponse(
            session_id=session.session_id,
            topic=session.topic,
            status=str(session.status),
            report_id=session.report_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            retrieval_latency_ms=metrics.retrieval_latency_ms,
            scraping_latency_ms=metrics.scraping_latency_ms,
            llm_latency_ms=metrics.llm_latency_ms,
            total_execution_ms=metrics.total_execution_ms,
            total_tokens=metrics.total_tokens,
            estimated_cost_usd=metrics.estimated_cost_usd,
        )

        verification_resp = None
        if verification:
            claims_resp = [
                VerifiedClaimResponse(
                    claim_text=c.claim_text,
                    # Serialize evidence safely — HttpUrl fields must be str
                    evidence=[
                        {**e.model_dump(mode="json"), "url": str(e.url)}
                        for e in c.evidence
                    ],
                    confidence=c.confidence,
                    status=str(c.status),
                )
                for c in verification.claims
            ]
            verification_resp = VerificationSummaryResponse(
                claims=claims_resp,
                # Normalise 0–100 → 0–1 for the frontend
                overall_verification_score=_normalise_verification_score(
                    verification.overall_verification_score
                ),
                verdict=verification.verdict,
            )

        brief = report.executive_brief
        brief_resp = None
        if brief:
            brief_resp = ExecutiveBriefResponse(
                executive_summary=brief.executive_summary,
                key_insights=brief.key_insights,
                risks=brief.risks,
                opportunities=brief.opportunities,
                recommended_actions=brief.recommended_actions,
            )

        topic_id_for_resp = "local"
        if request.persist_memory:
            try:
                topic_id_for_resp = repository.topics.get_or_create_topic(request.topic)
            except Exception:
                pass

        report_resp = ResearchReportResponse(
            report_id=report.report_id,
            session_id=session.session_id,
            topic_id=topic_id_for_resp,
            topic=report.topic,
            summary=report.summary,
            report_markdown=report.report_markdown,
            key_statistics=report.key_statistics,
            swot=report.swot if isinstance(report.swot, dict) else {},
            verification=verification_resp,
            executive_brief=brief_resp,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

        sources_resp = [
            SourceResponse(
                source_id=src.source_id,
                title=src.title,
                url=str(src.url),
                domain=src.domain,
                snippet=src.snippet,
                publish_date=str(src.publish_date) if src.publish_date else None,
                relevance_score=src.relevance_score,
                credibility_score=src.credibility_score,
                final_score=round(0.7 * src.relevance_score + 0.3 * src.credibility_score, 4),
                rank_position=src.rank_position,
            )
            for src in ranked_sources
        ]

        logger.info(
            "Research completed successfully",
            extra={
                "topic": request.topic,
                "report_id": report.report_id,
                "source_count": len(sources_resp),
                "verification_score": verification_resp.overall_verification_score if verification_resp else None,
            },
        )

        return ApiResponse(
            success=True,
            message="Research completed successfully",
            data=ResearchRunResponse(
                session=session_resp,
                report=report_resp,
                top_sources=sources_resp,
                verification=verification_resp,
            ),
        )

    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Research route failed", extra={"error": str(e), "traceback": tb})
        raise HTTPException(status_code=500, detail=str(e))
