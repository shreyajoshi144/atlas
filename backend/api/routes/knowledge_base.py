from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.memory import VectorMemoryService
from backend.models.response_models import (
    ApiResponse,
    ErrorResponse,
    KnowledgeBaseResponse,
)

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Report not found"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.get(
    "/knowledge-base",
    response_model=ApiResponse[KnowledgeBaseResponse],
    responses=ERROR_RESPONSES,
)
async def knowledge_base_overview(current_user: dict = Depends(get_current_user)) -> ApiResponse[KnowledgeBaseResponse]:
    try:
        repository = ResearchRepository()

        kb = repository.knowledge_base.get_overview(user_id=current_user["user_id"])
        # Use the SQL-derived, already user-scoped chunk count rather than
        # vector_memory.collection.count() — that call returns Chroma's raw
        # global collection size across *all* users, which would leak how
        # much data exists system-wide to every authenticated user.
        chroma_size = kb.get("total_chunks", 0)

        return ApiResponse(
            success=True,
            message="Knowledge base retrieved",
            data=KnowledgeBaseResponse(
                total_reports=kb.get("total_reports", 0),
                total_chunks=kb.get("total_chunks", 0),
                chroma_collection_size=chroma_size,
                recent_topics=kb.get("recent_topics", []),
                report_ids=kb.get("report_ids", []),
            ),
        )
    except Exception as exc:
        raise safe_server_error(logger, "Knowledge base query", exc) from exc


@router.delete(
    "/knowledge-base/report/{report_id}",
    response_model=ApiResponse[None],
    responses=ERROR_RESPONSES,
)
async def delete_report(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[None]:
    """
    Permanently deletes a report from SQLite and its chunks from ChromaDB.
    Frontend calls: DELETE /api/knowledge-base/report/{report_id}
    """
    try:
        repository = ResearchRepository()
        vector_memory = VectorMemoryService()

        # Ownership check — a report existing is not enough; it must belong
        # to the requesting user. Same 404 either way so a report ID can't be
        # used to probe whether it exists under someone else's account.
        if not repository.reports.is_owned_by(report_id, current_user["user_id"]):
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Delete from ChromaDB first — user_id enforced here too (belt and
        # suspenders alongside the is_owned_by check above).
        vector_memory.delete_report_memory(report_id=report_id, user_id=current_user["user_id"])

        # Delete from SQLite (also removes any note attached to this report)
        repository.reports.delete_report(report_id=report_id)

        return ApiResponse(
            success=True,
            message=f"Report {report_id} deleted successfully",
            data=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Report deletion", exc) from exc
