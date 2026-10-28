from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.response_models import (
    ApiResponse,
    ErrorResponse,
    UnansweredQuestionResponse,
    UnansweredQuestionsResponse,
    ResearchInsightsResponse,
)
from backend.services.questions_service import derive_unanswered_questions
from backend.services.research_insights_service import build_evidence_analysis
from backend.models.api_models import QuestionResearchRequest, ResearchRunRequest

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Report not found"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.get(
    "/{report_id}",
    response_model=ApiResponse[UnansweredQuestionsResponse],
    responses=ERROR_RESPONSES,
)
async def get_unanswered_questions(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[UnansweredQuestionsResponse]:
    """Derives "Questions I Still Need to Answer" from THIS report's own
    verification claims and sources — never from another user's data, and
    never invented beyond what those already-computed fields support. Same
    ownership pattern as notes.py/history.py: report_id is looked up
    scoped to current_user, so a report ID that exists but belongs to
    someone else is indistinguishable from one that doesn't exist at all.
    """
    try:
        repository = ResearchRepository()
        user_id = current_user["user_id"]

        report_row = repository.reports.get_report(report_id=report_id, user_id=user_id)
        if not report_row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        source_rows = repository.sources.get_sources_by_report_id(report_id)
        questions = derive_unanswered_questions(report_row, source_rows)

        return ApiResponse(
            success=True,
            message="Questions derived" if questions else "No unanswered questions could be derived from this report",
            data=UnansweredQuestionsResponse(
                report_id=report_id,
                topic=report_row.get("topic", ""),
                questions=[UnansweredQuestionResponse(**q) for q in questions],
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Deriving unanswered questions", exc) from exc


@router.get(
    "/{report_id}/insights",
    response_model=ApiResponse[ResearchInsightsResponse],
    responses=ERROR_RESPONSES,
)
async def get_research_insights(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[ResearchInsightsResponse]:
    """Evidence breakdown and research gaps derived only from this report."""
    try:
        repository = ResearchRepository()
        row = repository.reports.get_report(report_id=report_id, user_id=current_user["user_id"])
        if not row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
        sources = repository.sources.get_sources_by_report_id(report_id)
        return ApiResponse(success=True, message="Research insights derived", data=ResearchInsightsResponse(**build_evidence_analysis(row, sources)))
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Deriving research insights", exc) from exc


@router.post(
    "/{report_id}/research",
    response_model=ApiResponse,
    responses=ERROR_RESPONSES,
)
async def research_from_question(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    request: QuestionResearchRequest,
    current_user: dict = Depends(get_current_user),
):
    """Open a normal Atlas research run from a question/gap derived from this report."""
    try:
        repository = ResearchRepository()
        row = repository.reports.get_report(report_id=report_id, user_id=current_user["user_id"])
        if not row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
        followup = ResearchRunRequest(
            topic=request.question_text,
            research_mode="investigate",
            user_context=f"Follow-up research derived from the existing Atlas case: {row.get('topic', '')}",
            include_verification=True,
            include_executive_brief=True,
            persist_memory=True,
            top_k_urls=15,
            source_report_id=report_id,
        )
        from backend.api.routes.research import run_research
        return await run_research(followup, current_user)
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Researching follow-up question", exc) from exc
