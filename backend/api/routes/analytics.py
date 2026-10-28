from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.analytics.metrics_service import MetricsService
from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.models.response_models import (
    AnalyticsOverviewResponse,
    ApiResponse,
    ErrorResponse,
)

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


@router.get(
    "/overview",
    response_model=ApiResponse[AnalyticsOverviewResponse],
    responses=ERROR_RESPONSES,
)
async def analytics_overview(current_user: dict = Depends(get_current_user)) -> ApiResponse[AnalyticsOverviewResponse]:
    try:
        metrics = MetricsService()
        overview = metrics.get_overview(user_id=current_user["user_id"])
        return ApiResponse(
            success=True,
            message="Analytics retrieved",
            data=AnalyticsOverviewResponse(**overview),
        )
    except Exception as exc:
        raise safe_server_error(logger, "Analytics query", exc) from exc
