from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.api_models import NoteUpdateRequest
from backend.models.response_models import ApiResponse, ErrorResponse, NoteResponse

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Case not found"},
    422: {"description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _require_owned_report(repository: ResearchRepository, report_id: str, user_id: str) -> None:
    """Notes always belong to a specific case. Before touching a note we
    confirm the case itself belongs to this user — same 404 whether the
    report doesn't exist or belongs to someone else, so a report ID can't
    be used to probe for other users' research."""
    if not repository.reports.is_owned_by(report_id, user_id):
        raise HTTPException(status_code=404, detail=f"Case {report_id} not found")


@router.get(
    "/{report_id}",
    response_model=ApiResponse[NoteResponse],
    responses=ERROR_RESPONSES,
)
async def get_note(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[NoteResponse]:
    try:
        repository = ResearchRepository()
        user_id = current_user["user_id"]
        _require_owned_report(repository, report_id, user_id)

        note = repository.notes.get_note(user_id=user_id, report_id=report_id)
        return ApiResponse(
            success=True,
            message="Note retrieved",
            data=NoteResponse(
                report_id=report_id,
                content=note["content"] if note else "",
                updated_at=_parse_dt(note["updated_at"]) if note else None,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Loading note", exc) from exc


@router.put(
    "/{report_id}",
    response_model=ApiResponse[NoteResponse],
    responses=ERROR_RESPONSES,
)
async def save_note(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    request: NoteUpdateRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[NoteResponse]:
    try:
        repository = ResearchRepository()
        user_id = current_user["user_id"]
        _require_owned_report(repository, report_id, user_id)

        saved = repository.notes.upsert_note(user_id=user_id, report_id=report_id, content=request.content)
        return ApiResponse(
            success=True,
            message="Note saved",
            data=NoteResponse(
                report_id=report_id,
                content=saved["content"],
                updated_at=_parse_dt(saved["updated_at"]),
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Saving note", exc) from exc


@router.delete(
    "/{report_id}",
    response_model=ApiResponse[None],
    responses=ERROR_RESPONSES,
)
async def delete_note(
    report_id: Annotated[str, Path(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[None]:
    try:
        repository = ResearchRepository()
        user_id = current_user["user_id"]
        _require_owned_report(repository, report_id, user_id)

        repository.notes.delete_note(user_id=user_id, report_id=report_id)
        return ApiResponse(success=True, message="Note deleted", data=None)
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Deleting note", exc) from exc
