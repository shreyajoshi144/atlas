from __future__ import annotations

from fastapi import APIRouter

from backend.core.config import get_settings
from backend.models.api_models import SettingsUpdateRequest
from backend.models.response_models import ApiResponse


router = APIRouter()


@router.get("")
async def get_settings_route():
    settings = get_settings()
    return ApiResponse(
        success=True,
        message="Settings fetched",
        data={
            "app_name": settings.app_name,
            "app_env": settings.app_env,
            "openai_model": settings.openai_model,
            "tavily_max_results": settings.tavily_max_results,
            "default_top_k_urls": settings.default_top_k_urls,
            "scrape_timeout_seconds": settings.scrape_timeout_seconds,
            "scrape_max_retries": settings.scrape_max_retries,
        },
    )


@router.post("")
async def update_settings_route(request: SettingsUpdateRequest):
    return ApiResponse(
        success=True,
        message="Settings update acknowledged",
        data=request.model_dump(exclude_none=True),
    )