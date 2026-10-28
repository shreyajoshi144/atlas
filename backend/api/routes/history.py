from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.core.observability import serialize_public_trace
from backend.database import ResearchRepository
from backend.evaluation.models import EvaluationSummary
from backend.models.response_models import (
    ApiResponse,
    ErrorResponse,
    ExecutiveBriefResponse,
    HistoryReportItemResponse,
    HistoryReportsResponse,
    ReportDetailResponse,
    ResearchReportResponse,
    SourceResponse,
    VerificationSummaryResponse,
    VerifiedClaimResponse,
)

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    422: {"description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


def _load_research_trace(repository: ResearchRepository, session_id: str, user_id: str) -> list[dict[str, Any]]:
    """Return the sanitised persisted research trace for a report's session.

    Ownership is enforced by ``get_events_for_session`` (it joins on the
    session's user_id). Trace retrieval is best-effort: a missing or malformed
    trace must never prevent the report itself from being returned.
    """
    if not session_id:
        return []
    try:
        events = repository.analytics.get_events_for_session(session_id, user_id)
        for event in reversed(events):
            if event.get("event_type") != "agentic_trace":
                continue
            payload = json.loads(event.get("payload_json") or "{}")
            trace = payload.get("trace")
            if isinstance(trace, list):
                return serialize_public_trace(trace)
        return []
    except Exception as exc:  # trace is auxiliary — never fail the report fetch
        logger.warning("Loading research trace failed (non-fatal)", extra={"error": str(exc), "session_id": session_id})
        return []


def _normalise_score(score: float | None) -> float | None:
    """Domain model stores 0-100, frontend expects 0-1."""
    if score is None:
        return None
    return round(score / 100.0, 4) if score > 1.0 else round(score, 4)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.utcnow()


def _build_verification_resp(
    verification_json: str | None,
    verification_score: float | None,
    verification_verdict: str | None,
) -> VerificationSummaryResponse | None:
    if not verification_json:
        return None
    try:
        v = json.loads(verification_json)
        claims = [
            VerifiedClaimResponse(
                claim_text=c.get("claim_text", ""),
                evidence=c.get("evidence", []),
                confidence=c.get("confidence", 0.0),
                status=c.get("status", ""),
            )
            for c in v.get("claims", [])
        ]
        raw_score = v.get("overall_verification_score", verification_score or 0.0)
        return VerificationSummaryResponse(
            claims=claims,
            overall_verification_score=_normalise_score(raw_score) or 0.0,
            verdict=v.get("verdict", verification_verdict or ""),
        )
    except Exception:
        return None


def _build_executive_brief_resp(brief_json: str | None) -> ExecutiveBriefResponse | None:
    if not brief_json:
        return None
    try:
        b = json.loads(brief_json)
        return ExecutiveBriefResponse(
            executive_summary=b.get("executive_summary", ""),
            key_insights=b.get("key_insights", []),
            risks=b.get("risks", []),
            opportunities=b.get("opportunities", []),
            recommended_actions=b.get("recommended_actions", []),
        )
    except Exception:
        return None


def _build_evaluation_resp(evaluation_row: dict[str, Any] | None) -> EvaluationSummary | None:
    """Reconstructs the stored EvaluationSummary from its persisted
    details_json. Never raises — a malformed/missing row just means no
    evaluation is shown, exactly like a report with no verification pass."""
    if not evaluation_row or not evaluation_row.get("details_json"):
        return None
    try:
        return EvaluationSummary.model_validate_json(evaluation_row["details_json"])
    except Exception:
        return None


@router.get(
    "/reports",
    response_model=ApiResponse[HistoryReportsResponse],
    responses=ERROR_RESPONSES,
)
async def list_reports(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    topic: Annotated[str | None, Query(max_length=300)] = None,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[HistoryReportsResponse]:
    try:
        repository = ResearchRepository()
        user_id = current_user["user_id"]
        rows = repository.reports.list_reports(limit=limit, offset=offset, topic=topic, user_id=user_id)
        total = repository.reports.count_reports(topic=topic, user_id=user_id)

        reports = []
        for row in rows:
            created_at = _parse_datetime(row.get("created_at")) or datetime.utcnow()
            updated_at = _parse_datetime(row.get("updated_at")) or created_at
            reports.append(
                HistoryReportItemResponse(
                    report_id=row["report_id"],
                    session_id=row.get("session_id", ""),
                    topic_id=row.get("topic_id", ""),
                    topic=row["topic"],
                    summary=row.get("summary"),
                    verification_score=_normalise_score(row.get("verification_score")),
                    verification_verdict=row.get("verification_verdict"),
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

        return ApiResponse(
            success=True,
            message="Reports retrieved",
            data=HistoryReportsResponse(
                reports=reports,
                total=total,
                limit=limit,
                offset=offset,
            ),
        )
    except Exception as exc:
        raise safe_server_error(logger, "Listing reports", exc) from exc


@router.get(
    "/report/{report_id}",
    response_model=ApiResponse[ReportDetailResponse],
    responses=ERROR_RESPONSES,
)
async def get_report(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[ReportDetailResponse]:
    try:
        repository = ResearchRepository()
        row = repository.reports.get_report(report_id=report_id, user_id=current_user["user_id"])
        if not row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        created_at = _parse_datetime(row.get("created_at")) or datetime.utcnow()
        updated_at = _parse_datetime(row.get("updated_at")) or created_at

        verification_resp = _build_verification_resp(
            row.get("verification_json"),
            row.get("verification_score"),
            row.get("verification_verdict"),
        )
        brief_resp = _build_executive_brief_resp(row.get("executive_brief_json"))

        key_statistics: list[str] = []
        swot: dict[str, list[str]] = {}
        try:
            key_statistics = json.loads(row.get("key_statistics_json") or "[]")
        except Exception:
            pass
        try:
            swot = json.loads(row.get("swot_json") or "{}")
        except Exception:
            pass

        report_resp = ResearchReportResponse(
            report_id=row["report_id"],
            session_id=row.get("session_id", ""),
            topic_id=row.get("topic_id", ""),
            topic=row["topic"],
            summary=row.get("summary", ""),
            report_markdown=row.get("report_markdown", ""),
            key_statistics=key_statistics,
            swot=swot,
            verification=verification_resp,
            executive_brief=brief_resp,
            created_at=created_at,
            updated_at=updated_at,
        )

        source_rows = repository.sources.get_sources_by_report_id(report_id)
        sources_resp = [
            SourceResponse(
                source_id=s["source_id"],
                title=s["title"],
                url=s["url"],
                domain=s["domain"],
                snippet=s.get("snippet", ""),
                publish_date=s.get("publish_date"),
                relevance_score=s.get("relevance_score", 0.0),
                credibility_score=s.get("credibility_score", 0.0),
                final_score=round(
                    0.7 * s.get("relevance_score", 0.0)
                    + 0.3 * s.get("credibility_score", 0.0),
                    4,
                ),
                rank_position=s.get("rank_position", 0),
            )
            for s in source_rows
        ]

        evaluation_row = repository.evaluations.get_latest_by_report_id(report_id, user_id=current_user["user_id"])
        evaluation_resp = _build_evaluation_resp(evaluation_row)
        research_trace = _load_research_trace(repository, row.get("session_id", ""), current_user["user_id"])

        return ApiResponse(
            success=True,
            message="Report retrieved",
            data=ReportDetailResponse(report=report_resp, sources=sources_resp, evaluation=evaluation_resp, research_trace=research_trace),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Fetching report", exc) from exc
