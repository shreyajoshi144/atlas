from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit_by_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.memory import VectorMemoryService
from backend.models.api_models import SemanticSearchRequest
from backend.models.response_models import (
    ApiResponse,
    ErrorResponse,
    SemanticSearchResponse,
    SemanticSearchResultResponse,
)

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    422: {"description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.post(
    "/semantic",
    response_model=ApiResponse[SemanticSearchResponse],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit_by_user("semantic_search", limit=30, window_seconds=300))],
)
async def semantic_search(
    request: SemanticSearchRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[SemanticSearchResponse]:
    try:
        user_id = current_user["user_id"]
        vector_memory = VectorMemoryService()
        repository = ResearchRepository()

        raw_results = vector_memory.search_similar(
            query=request.query,
            user_id=user_id,
            limit=request.limit,
            topic=request.topic,
        )

        results: list[SemanticSearchResultResponse] = []
        seen_report_ids: set[str] = set()

        for item in raw_results:
            report_id = item.get("report_id", "")
            if report_id in seen_report_ids:
                continue
            seen_report_ids.add(report_id)

            topic = item.get("topic", "")
            similarity_score = item.get("similarity_score", 0.0)

            summary = ""
            try:
                row = repository.reports.get_report(report_id, user_id=user_id)
                if row:
                    summary = row.get("summary", "")
                    topic = row.get("topic", topic)
            except Exception:
                pass

            results.append(
                SemanticSearchResultResponse(
                    report_id=report_id,
                    topic=topic,
                    summary=summary,
                    similarity_score=round(similarity_score, 4),
                )
            )

        return ApiResponse(
            success=True,
            message=f"Found {len(results)} results",
            data=SemanticSearchResponse(results=results),
        )
    except Exception as exc:
        raise safe_server_error(logger, "Semantic search", exc) from exc
