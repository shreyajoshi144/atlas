from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from backend.database import ResearchRepository
from backend.memory import VectorMemoryService
from backend.models.response_models import (
    ApiResponse,
    ErrorResponse,
    KnowledgeBaseResponse,
)

router = APIRouter()

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Report not found"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.get(
    "/knowledge-base",
    response_model=ApiResponse[KnowledgeBaseResponse],
    responses=ERROR_RESPONSES,
)
async def knowledge_base_overview() -> ApiResponse[KnowledgeBaseResponse]:
    try:
        repository = ResearchRepository()
        vector_memory = VectorMemoryService()

        kb = repository.knowledge_base.get_overview()
        chroma_size = vector_memory.collection.count()

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
        raise HTTPException(
            status_code=500, detail=f"Knowledge base query failed: {exc}"
        ) from exc


@router.delete(
    "/knowledge-base/report/{report_id}",
    response_model=ApiResponse[None],
    responses=ERROR_RESPONSES,
)
async def delete_report(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
) -> ApiResponse[None]:
    """
    Permanently deletes a report from SQLite and its chunks from ChromaDB.
    Frontend calls: DELETE /api/knowledge-base/report/{report_id}
    """
    try:
        repository = ResearchRepository()
        vector_memory = VectorMemoryService()

        report = repository.reports.get_report(report_id=report_id)
        if not report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Delete from ChromaDB first
        vector_memory.delete_report_memory(report_id=report_id)

        # Delete from SQLite
        repository.reports.delete_report(report_id=report_id)

        return ApiResponse(
            success=True,
            message=f"Report {report_id} deleted successfully",
            data=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Delete failed: {exc}"
        ) from exc
