from __future__ import annotations

import asyncio
import time
import traceback
import json
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit_by_user, acquire_research_run, release_research_run
from backend.core.errors import safe_server_error
from backend.graph.workflow import build_research_graph
from backend.models.api_models import ResearchRunRequest
from backend.models.response_models import (
    ApiResponse, ErrorResponse, ExecutiveBriefResponse,
    ResearchRunResponse, ResearchReportResponse, ResearchSessionResponse,
    SourceResponse, VerificationSummaryResponse, VerifiedClaimResponse, ResearchInsightsResponse,
)
from backend.core.logging import get_logger
from backend.routing.context import in_current_context, routing_scope
from backend.core.observability import append_trace, serialize_public_trace, serialize_public_agentic_trace, serialize_public_tool_trace
from backend.evaluation import run_pipeline_evaluation
from backend.services.research_insights_service import build_evidence_analysis

logger = get_logger(__name__)
router = APIRouter()

MODE_DEFAULT_TOP_K = {
    "brief": 8,
    "explore": 12,
    "compare": 14,
    "investigate": 20,
    "literature_review": 20,
}

def _resolve_top_k_urls(request: ResearchRunRequest) -> int:
    return request.top_k_urls if request.top_k_urls is not None else MODE_DEFAULT_TOP_K[request.research_mode]

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
    # NOTE: must match the import used in chat.py and search.py
    # (backend.memory.retrieval_service does not exist — that path is the
    # unrelated Tavily/ranking RetrievalService under backend.services).
    # The wrong path here silently broke vector indexing on every research
    # run: index_report() always raised ModuleNotFoundError, which was
    # swallowed by the non-fatal except block below, so Chroma was never
    # populated and Discover/Chat always saw an empty collection.
    from backend.memory import VectorMemoryService
    return VectorMemoryService()


def _normalise_verification_score(score: float) -> float:
    """Domain model stores verification score as 0–100. API/frontend expects 0–1."""
    if score > 1.0:
        return round(score / 100.0, 4)
    return round(score, 4)


@router.post(
    "/run",
    response_model=ApiResponse[ResearchRunResponse],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit_by_user("research_run", limit=5, window_seconds=300))],
)
async def run_research(
    request: ResearchRunRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[ResearchRunResponse]:
    acquire_research_run(current_user["user_id"])
    try:
        # "Research this question": if this run was triggered from a
        # derived unanswered question, source_report_id names where it
        # came from. Never trusted as-is — re-verified against the
        # authenticated user's own reports here, same is_owned_by check
        # notes.py/history.py use, so it can't be used to reference (or
        # imply access to) another user's report.
        verified_source_report_id: str | None = None
        if request.source_report_id:
            if _repository().reports.is_owned_by(request.source_report_id, current_user["user_id"]):
                verified_source_report_id = request.source_report_id
            else:
                logger.warning(
                    "Ignoring source_report_id not owned by requesting user",
                    extra={"source_report_id": request.source_report_id},
                )

        graph = build_research_graph()
        requested_top_k = _resolve_top_k_urls(request)
        logger.info(
            "Research request source target",
            extra={
                "research_mode": request.research_mode,
                "requested_top_k": requested_top_k,
                "explicit_top_k": request.top_k_urls is not None,
            },
        )
        initial_state = {
            "top_k_urls": requested_top_k,
            "research_mode": request.research_mode,
            "search_query": request.topic,
            "topic": request.topic,
            "user_context": request.user_context,
            "authenticated_user_id": current_user["user_id"],
            "include_verification": request.include_verification,
            "include_executive_brief": request.include_executive_brief,
            "persist_memory": request.persist_memory,
            "debug": {"started_at": time.perf_counter()},
            "errors": [],
        }

        loop = asyncio.get_event_loop()
        # M9: model calls made by the graph are routed per user. ``run_in_executor`` does not carry
        # context variables into its worker thread, so hand the scope over explicitly (agents unchanged).
        with routing_scope(current_user["user_id"], "research"):
            result_state = await loop.run_in_executor(None, in_current_context(graph.invoke), initial_state)

        session = result_state["session"]
        report = result_state["final_report"]
        ranked_sources = result_state.get("ranked_sources", [])
        verification = result_state.get("verification_summary")
        verification_status = result_state.get("verification_status", "unavailable")

        result_state["research_trace"] = append_trace(result_state, component="evaluation", action="evaluate", status="started")

        # ── Evaluation layer (non-blocking) ──────────────────────────────────
        # Runs entirely in-process against data the graph already produced —
        # no extra LLM/network calls — and can never fail the research run:
        # run_pipeline_evaluation() already isolates each evaluator, and this
        # try/except is a second line of defense around the whole call.
        evaluation_summary = None
        try:
            evaluation_summary = run_pipeline_evaluation(
                topic=request.topic,
                search_query=request.topic,
                requested_top_k=requested_top_k,
                ranked_sources=ranked_sources,
                evidence_text=result_state.get("evidence_text", ""),
                verification_summary=verification,
                verification_status=verification_status,
                report_markdown=report.report_markdown,
                key_statistics=report.key_statistics,
                report_id=report.report_id,
                session_id=session.session_id,
                research_plan=result_state.get("research_plan"),
                tool_trace=serialize_public_tool_trace(result_state.get("tool_trace", [])),
                replan_count=result_state.get("replan_count", 0),
                replan_history=result_state.get("replan_history", []),
                agentic_trace=serialize_public_agentic_trace(result_state.get("agentic_trace", [])),
                completed_action_ids=result_state.get("completed_action_ids", []),
                tool_call_count=result_state.get("tool_call_count", 0),
                agentic_status=result_state.get("agentic_status"),
                execution_metrics=result_state.get("execution_metrics", {}),
                total_latency_ms=getattr(result_state.get("metrics"), "total_execution_ms", None),
                node_latency_ms={
                    "retrieval": getattr(result_state.get("metrics"), "retrieval_latency_ms", 0),
                    "scraping": getattr(result_state.get("metrics"), "scraping_latency_ms", 0),
                    "llm": getattr(result_state.get("metrics"), "llm_latency_ms", 0),
                },
                token_usage={
                    "prompt_tokens": getattr(result_state.get("metrics"), "prompt_tokens", 0),
                    "completion_tokens": getattr(result_state.get("metrics"), "completion_tokens", 0),
                    "total_tokens": getattr(result_state.get("metrics"), "total_tokens", 0),
                },
                estimated_cost_usd=getattr(result_state.get("metrics"), "estimated_cost_usd", 0.0),
            )
        except Exception as eval_exc:
            logger.warning(
                "Evaluation layer failed entirely (non-fatal)",
                extra={"error": str(eval_exc), "report_id": report.report_id},
            )

        result_state["research_trace"] = append_trace(
            result_state,
            component="evaluation",
            action="evaluate",
            status="succeeded" if evaluation_summary is not None else "failed",
            metadata={"available": evaluation_summary is not None},
        )
        result_state["research_trace"] = append_trace(
            result_state,
            component="finalization",
            action="api_finalize",
            status="succeeded",
            metadata={"report_id": report.report_id},
        )

        # ── Persist to SQLite + ChromaDB ─────────────────────────────────────
        repository = _repository()
        if request.persist_memory:
            try:
                topic_id = repository.topics.get_or_create_topic(request.topic, user_id=current_user["user_id"])
                # save_session expects (topic_id, session); user_id stamps
                # ownership so history/search/analytics/notes can be scoped
                # to the person who ran this research.
                repository.sessions.save_session(topic_id, session, user_id=current_user["user_id"])
                # save_report expects (topic_id, session, report)
                repository.reports.save_report(topic_id, session, report)
                # NOTE: previously looped save_source() per source, which hardcoded
                # session_id="" and violated the sources table's FK constraint on
                # every call — the resulting IntegrityError aborted this entire
                # try block, silently dropping ALL sources for EVERY research run
                # (report/verification/claims still saved fine since save_report
                # ran first). replace_sources() correctly threads the real
                # session_id and does one bulk delete+insert instead of N calls.
                repository.sources.replace_sources(report.report_id, session.session_id, ranked_sources)
                if evaluation_summary is not None:
                    try:
                        repository.evaluations.save_evaluation(
                            report.report_id, session.session_id, topic_id, evaluation_summary
                        )
                    except Exception as eval_db_exc:
                        logger.warning(
                            "Saving evaluation failed (non-fatal)",
                            extra={"error": str(eval_db_exc), "report_id": report.report_id},
                        )
                try:
                    # M5: persist Source/Evidence/Claim/Entity + relationships (atomic, non-fatal).
                    from backend.knowledge.service import KnowledgeService

                    verification_for_graph = verification or getattr(report, "verification", None)
                    KnowledgeService(repository).record_investigation(
                        current_user["user_id"],
                        report.report_id,
                        ranked_sources,
                        verification_for_graph.claims if verification_for_graph else [],
                        scraped_contents=result_state.get("scraped_contents", []) or [],
                    )
                except Exception as knowledge_exc:
                    logger.warning(
                        "Recording knowledge graph failed (non-fatal)",
                        extra={"error": str(knowledge_exc), "report_id": report.report_id},
                    )
                try:
                    from backend.models.common import EventType
                    from backend.models.domain_models import AnalyticsEvent
                    repository.analytics.save_event(
                        AnalyticsEvent(
                            session_id=session.session_id,
                            event_type=EventType.AGENTIC_TRACE,
                            payload={
                                "user_id": current_user["user_id"],
                                "agentic_status": result_state.get("agentic_status"),
                                "verification_status": result_state.get("agentic_verification_status", "not_run"),
                                "replan_count": result_state.get("replan_count", 0),
                                "trace": result_state.get("research_trace", []),
                                "tool_trace": result_state.get("tool_trace", []),
                                "tool_call_count": result_state.get("tool_call_count", 0),
                            },
                        )
                    )
                except Exception as trace_exc:
                    logger.warning(
                        "Saving research trace failed (non-fatal)",
                        extra={"error": str(trace_exc), "session_id": session.session_id},
                    )
                try:
                    _vector_memory().index_report(
                        topic_id=topic_id,
                        session=session,
                        report=report,
                        user_id=current_user["user_id"],
                    )
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
                topic_id_for_resp = repository.topics.get_or_create_topic(
                    request.topic, user_id=current_user["user_id"]
                )
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

        analysis_source_rows = [
            {
                "source_id": src.source_id,
                "title": src.title,
                "url": str(src.url),
                "domain": src.domain,
                "publish_date": str(src.publish_date) if src.publish_date else None,
            }
            for src in ranked_sources
        ]
        analysis_report_row = {
            "verification_json": json.dumps(verification.model_dump(mode="json")) if verification else None,
        }
        insights_resp = ResearchInsightsResponse(**build_evidence_analysis(analysis_report_row, analysis_source_rows))

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
                insights=insights_resp,
                source_report_id=verified_source_report_id,
                verification_status=verification_status,
                evaluation=evaluation_summary,
                agentic_status=result_state.get("agentic_status"),
                agentic_verification_status=result_state.get("agentic_verification_status"),
                replan_count=result_state.get("replan_count", 0),
                agentic_trace=serialize_public_agentic_trace(result_state.get("agentic_trace", [])),
                tool_trace=serialize_public_tool_trace(result_state.get("tool_trace", [])),
                research_trace=serialize_public_trace(result_state.get("research_trace", [])),
            ),
        )

    except Exception as e:
        raise safe_server_error(logger, "Research run", e) from e
    finally:
        release_research_run(current_user["user_id"])