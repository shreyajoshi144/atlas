from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.analytics.metrics_service import MetricsService
from backend.models.response_models import (
    AnalyticsOverviewResponse,
    ApiResponse,
    ErrorResponse,
)

router = APIRouter()

ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.get(
    "/overview",
    response_model=ApiResponse[AnalyticsOverviewResponse],
    responses=ERROR_RESPONSES,
)
async def analytics_overview() -> ApiResponse[AnalyticsOverviewResponse]:
    try:
        metrics = MetricsService()
        overview = metrics.get_overview()
        return ApiResponse(
            success=True,
            message="Analytics retrieved",
            data=AnalyticsOverviewResponse(**overview),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analytics failed: {exc}") from exc
