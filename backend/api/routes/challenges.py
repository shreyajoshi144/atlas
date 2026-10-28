"""Challenge API (M7): adversarially test a claim from an investigation.

All endpoints are authenticated and scoped to the caller. A challenge, claim or
investigation that does not exist *or belongs to another user* yields the same
404. Execution is rate-limited per user and idempotent: executing a completed
challenge returns its stored result without re-running; a failed challenge can
be retried (bounded). Raw error messages never leave the server — only the
exception class name.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit_by_user
from backend.challenges.models import CHALLENGE_TYPES, RESULTS, STATUSES
from backend.challenges.repository import ChallengeConflict, ChallengeNotFound
from backend.challenges.service import ChallengeService, ChallengeStateError
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.common import BaseSchema
from backend.models.response_models import ApiResponse, ErrorResponse
from pydantic import Field

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict (state does not allow this action)"},
    429: {"model": ErrorResponse, "description": "Rate limited"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}

ChallengeId = Annotated[str, Path(pattern=r"^chl_[0-9a-f]{32}$")]


class CreateChallengeRequest(BaseSchema):
    investigation_id: str = Field(min_length=3, max_length=120)
    claim_id: str = Field(pattern=r"^clm_[0-9a-f]{32}$")
    challenge_type: Literal[
        "counter_evidence", "contradiction_search", "source_quality",
        "temporal_validity", "assumption_check", "alternative_explanation",
    ]


def _service() -> ChallengeService:
    return ChallengeService(ResearchRepository())


def _run(action: str, fn):
    try:
        return fn()
    except ChallengeNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except (ChallengeConflict, ChallengeStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, action, exc) from exc


@router.post("", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def create_challenge(request: CreateChallengeRequest, current_user: dict = Depends(get_current_user)):
    """Create a challenge (idempotent while an identical one is pending/running)."""
    view, created = _run("Creating challenge", lambda: _service().create(
        current_user["user_id"], request.investigation_id, request.claim_id, request.challenge_type))
    return ApiResponse(success=True, message="Challenge created" if created else "An identical challenge is already active",
                       data={"challenge": view, "created": created})


@router.post("/{challenge_id}/execute", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES,
             dependencies=[Depends(rate_limit_by_user("challenge_execute", limit=10, window_seconds=300))])
async def execute_challenge(challenge_id: ChallengeId, current_user: dict = Depends(get_current_user)):
    """Run the challenge pipeline. Always returns the challenge; ``success`` is true only if it completed."""
    view, ran = _run("Executing challenge", lambda: _service().execute(current_user["user_id"], challenge_id))
    ok = view["status"] == "completed"
    message = ("Challenge completed" if ran else "Challenge already completed") if ok else "Challenge failed"
    return ApiResponse(success=ok, message=message, data={"challenge": view, "ran": ran})


@router.get("", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def list_challenges(
    investigation_id: Annotated[Optional[str], Query(min_length=3, max_length=120)] = None,
    claim_id: Annotated[Optional[str], Query(pattern=r"^clm_[0-9a-f]{32}$")] = None,
    status: Annotated[Optional[str], Query(pattern="^(" + "|".join(STATUSES) + ")$")] = None,
    result: Annotated[Optional[str], Query(pattern="^(" + "|".join(RESULTS) + ")$")] = None,
    challenge_type: Annotated[Optional[str], Query(pattern="^(" + "|".join(CHALLENGE_TYPES) + ")$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    current_user: dict = Depends(get_current_user),
):
    data = _run("Listing challenges", lambda: _service().list(
        current_user["user_id"], investigation_id=investigation_id, claim_id=claim_id, status=status, result=result,
        challenge_type=challenge_type, limit=limit, offset=offset))
    return ApiResponse(success=True, message="Challenges", data=data)


@router.get("/{challenge_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def get_challenge(challenge_id: ChallengeId, current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Challenge", data={"challenge": _run("Challenge lookup", lambda: _service().get(current_user["user_id"], challenge_id))})


@router.get("/{challenge_id}/inspect", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def inspect_challenge(challenge_id: ChallengeId, current_user: dict = Depends(get_current_user)):
    """Full result: queries, retrieved sources, evidence with provenance, classification
    factors, attempt history, safe trace, and the Claim → … → Updated interpretation trail."""
    return ApiResponse(success=True, message="Challenge detail", data=_run("Challenge inspection", lambda: _service().inspect(current_user["user_id"], challenge_id)))
