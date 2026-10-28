"""Routing API (M9): policies, decision preview, usage and cost.

Authenticated and user-scoped: a policy that does not exist *or belongs to another user* is the
same 404; usage rows are only ever those of the caller. Nothing here calls a model.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, ValidationError

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit_by_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.common import BaseSchema
from backend.models.response_models import ApiResponse, ErrorResponse
from backend.routing.models import POLICY_ID_PATTERN, PURPOSES, USAGE_STATUSES, RoutingPolicy
from backend.routing.repository import RoutingConflict, RoutingNotFound
from backend.routing.service import RoutingService

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict (duplicate, limit, or policy is active)"},
    429: {"model": ErrorResponse, "description": "Rate limited"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}
PolicyId = Annotated[str, Path(pattern=POLICY_ID_PATTERN)]


class ActiveRequest(BaseSchema):
    policy_id: Optional[str] = Field(default=None, pattern=POLICY_ID_PATTERN)     # null → back to the application default


class PreviewRequest(BaseSchema):
    policy_id: Optional[str] = Field(default=None, pattern=POLICY_ID_PATTERN)
    policy: Optional[RoutingPolicy] = None
    prompt_tokens: int = Field(default=2000, ge=1, le=200_000)


def _service() -> RoutingService:
    return RoutingService(ResearchRepository())


def _run(action: str, fn):
    try:
        return fn()
    except RoutingNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except RoutingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False, include_context=False)[0]
        raise HTTPException(status_code=400, detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"[:500]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:500]) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, action, exc) from exc


@router.get("/overview", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def overview(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Routing overview", data=_run("Routing overview", lambda: _service().overview(current_user["user_id"])))


@router.get("/policies", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def list_policies(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Policies", data=_run("Listing policies", lambda: _service().list_policies(current_user["user_id"])))


@router.post("/policies", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def create_policy(request: RoutingPolicy, current_user: dict = Depends(get_current_user)):
    data = _run("Creating policy", lambda: _service().create_policy(current_user["user_id"], request.model_dump(mode="json")))
    return ApiResponse(success=True, message="Policy created", data={"policy": data})


@router.delete("/policies/{policy_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def delete_policy(policy_id: PolicyId, current_user: dict = Depends(get_current_user)):
    _run("Deleting policy", lambda: _service().delete_policy(current_user["user_id"], policy_id))
    return ApiResponse(success=True, message="Policy deleted", data={"policy_id": policy_id})


@router.put("/active", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def set_active(request: ActiveRequest, current_user: dict = Depends(get_current_user)):
    """Activate a saved policy, or send ``{"policy_id": null}`` to return to the application default."""
    data = _run("Setting active policy", lambda: _service().set_active(current_user["user_id"], request.policy_id))
    return ApiResponse(success=True, message="Active policy updated", data=data)


@router.post("/preview", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES,
             dependencies=[Depends(rate_limit_by_user("routing_preview", limit=60, window_seconds=300))])
def preview(request: PreviewRequest, current_user: dict = Depends(get_current_user)):
    """Which model would be chosen, and why, for a call of this size — without calling any model."""
    data = _run("Routing preview", lambda: _service().preview(
        current_user["user_id"], policy_id=request.policy_id, policy=request.policy.model_dump(mode="json") if request.policy else None, prompt_tokens=request.prompt_tokens))
    return ApiResponse(success=True, message="Routing preview", data=data)


@router.get("/usage", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def usage(model: Annotated[Optional[str], Query(max_length=100, pattern=r"^[A-Za-z0-9._/:-]+$")] = None,
          purpose: Annotated[Optional[str], Query(pattern="^(" + "|".join(PURPOSES) + ")$")] = None,
          status: Annotated[Optional[str], Query(pattern="^(" + "|".join(USAGE_STATUSES) + ")$")] = None,
          fallback_only: bool = False, run_ref: Annotated[Optional[str], Query(pattern=r"^[0-9a-f]{16}$")] = None,
          limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
          current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Usage", data=_run("Listing usage", lambda: _service().list_usage(
        current_user["user_id"], model=model, purpose=purpose, status=status, fallback_only=fallback_only, run_ref=run_ref, limit=limit, offset=offset)))


@router.get("/usage/summary", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def usage_summary(days: Annotated[int, Query(ge=1, le=365)] = 30, current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Usage summary", data=_run("Usage summary", lambda: _service().usage_summary(current_user["user_id"], days)))
