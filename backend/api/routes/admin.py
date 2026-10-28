from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from backend.auth.dependencies import require_admin_user
from backend.core.config import get_settings
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.common import BaseSchema
from backend.models.response_models import ApiResponse
from backend.ops.repository import OperationalRepository

router = APIRouter()
logger = get_logger(__name__)


class UsageLimitsRequest(BaseSchema):
    daily_call_limit: int | None = Field(default=None, ge=0, le=1_000_000)
    monthly_spend_cap_usd: float | None = Field(default=None, ge=0, le=1_000_000)


def _repo() -> OperationalRepository:
    return OperationalRepository(ResearchRepository().db)


def _run(label: str, fn):
    try:
        return fn()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, label, exc) from exc


@router.get("/monitoring", response_model=ApiResponse[dict[str, Any]])
def monitoring(days: Annotated[int, Query(ge=1, le=90)] = 30,
               admin: dict = Depends(require_admin_user)):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data = _run("Loading operational monitoring", lambda: _repo().monitoring(since))
    data["window_days"] = days
    return ApiResponse(success=True, message="Operational monitoring", data=data)


@router.get("/users", response_model=ApiResponse[dict[str, Any]])
def users(limit: Annotated[int, Query(ge=1, le=200)] = 100,
          offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
          admin: dict = Depends(require_admin_user)):
    items, total = _run("Listing operational users", lambda: _repo().list_users(limit, offset))
    return ApiResponse(success=True, message="Users", data={"items": items, "total": total, "limit": limit, "offset": offset})


@router.get("/users/{user_id}/limits", response_model=ApiResponse[dict[str, Any]])
def get_user_limits(user_id: str, admin: dict = Depends(require_admin_user)):
    settings = get_settings()
    data = _run("Reading usage limits", lambda: _repo().effective_limits(user_id, settings.default_daily_llm_call_limit,
                                                                          settings.default_monthly_llm_spend_cap_usd))
    return ApiResponse(success=True, message="Usage limits", data=data)


@router.put("/users/{user_id}/limits", response_model=ApiResponse[dict[str, Any]])
def set_user_limits(user_id: str, request: UsageLimitsRequest, admin: dict = Depends(require_admin_user)):
    settings = get_settings()
    def update():
        _repo().set_limits(user_id, daily_call_limit=request.daily_call_limit,
                           monthly_spend_cap_usd=request.monthly_spend_cap_usd,
                           actor_id=admin["user_id"])
        return _repo().effective_limits(user_id, settings.default_daily_llm_call_limit,
                                        settings.default_monthly_llm_spend_cap_usd)
    data = _run("Updating usage limits", update)
    return ApiResponse(success=True, message="Usage limits updated", data=data)


@router.get("/audit", response_model=ApiResponse[dict[str, Any]])
def audit(limit: Annotated[int, Query(ge=1, le=200)] = 100,
          offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
          admin: dict = Depends(require_admin_user)):
    items, total = _run("Reading admin audit logs", lambda: _repo().audit_logs(limit, offset))
    return ApiResponse(success=True, message="Admin audit logs", data={"items": items, "total": total, "limit": limit, "offset": offset})
