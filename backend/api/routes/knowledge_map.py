"""Knowledge Map API (M6) — read-only exploration of the persisted knowledge graph.

Every endpoint is authenticated and scoped to the caller's own data. An ID that
does not exist *or belongs to another user* yields the same 404, so existence is
never leaked. Knowledge state (complete / partial / failed / not_recorded) is
present on every object, and the comparison endpoint refuses investigations whose
knowledge failed to persist.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.auth.dependencies import get_current_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.knowledge.map_service import ChangeComparisonError, KnowledgeMapService, NotFoundError
from backend.models.response_models import ApiResponse, ErrorResponse

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Investigation knowledge is not comparable (failed / not recorded)"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}

_ID_PATTERN = r"^(ent|clm|evd|src|chl|cev)_[0-9a-f]{32}$"
EntityId = Annotated[str, Path(pattern=r"^ent_[0-9a-f]{32}$")]
ClaimId = Annotated[str, Path(pattern=r"^clm_[0-9a-f]{32}$")]
EvidenceId = Annotated[str, Path(pattern=r"^evd_[0-9a-f]{32}$")]
SourceId = Annotated[str, Path(pattern=r"^src_[0-9a-f]{32}$")]
ReportId = Annotated[str, Path(min_length=3, max_length=120)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]
QueryText = Annotated[str, Query(max_length=200)]
Status = Annotated[Optional[str], Query(pattern="^(verified|partially_verified|not_verified|conflicting|insufficient_evidence)$")]


def _service() -> KnowledgeMapService:
    return KnowledgeMapService(ResearchRepository())


def _ok(message: str, data: Any) -> ApiResponse[dict[str, Any]]:
    return ApiResponse(success=True, message=message, data=data)


def _run(action: str, fn):
    """Map domain errors to HTTP errors; anything unexpected becomes a safe 500."""
    try:
        return fn()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ChangeComparisonError as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, action, exc) from exc


@router.get("/summary", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def summary(current_user: dict = Depends(get_current_user)):
    return _ok("Knowledge map summary", _run("Knowledge map summary", lambda: _service().summary(current_user["user_id"])))


@router.get("/investigations", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def investigations(current_user: dict = Depends(get_current_user)):
    items = _run("Listing investigations", lambda: _service().list_investigations(current_user["user_id"]))
    return _ok("Investigations", {"items": items, "total": len(items)})


@router.post("/investigations/{report_id}/rebuild", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def rebuild(report_id: ReportId, current_user: dict = Depends(get_current_user)):
    """Re-derive an investigation's knowledge from its stored sources/verification
    (recovers ``failed`` and ``not_recorded`` states)."""
    return _ok("Knowledge rebuilt", _run("Rebuilding knowledge", lambda: _service().rebuild(current_user["user_id"], report_id)))


@router.get("/entities", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def entities(query: QueryText = "", limit: Limit = 25, offset: Offset = 0, current_user: dict = Depends(get_current_user)):
    return _ok("Entities", _run("Listing entities", lambda: _service().list_entities(current_user["user_id"], query=query, limit=limit, offset=offset)))


@router.get("/entities/{entity_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def entity(entity_id: EntityId, current_user: dict = Depends(get_current_user)):
    return _ok("Entity", _run("Entity lookup", lambda: _service().get_entity(current_user["user_id"], entity_id)))


@router.get("/claims", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def claims(
    query: QueryText = "", status: Status = None, stale: Optional[bool] = None, conflicting: Optional[bool] = None,
    report_id: Annotated[Optional[str], Query(min_length=3, max_length=120)] = None,
    entity_id: Annotated[Optional[str], Query(pattern=r"^ent_[0-9a-f]{32}$")] = None,
    limit: Limit = 25, offset: Offset = 0, current_user: dict = Depends(get_current_user),
):
    return _ok("Claims", _run("Listing claims", lambda: _service().list_claims(
        current_user["user_id"], query=query, status=status, stale=stale, conflicting=conflicting,
        report_id=report_id, entity_id=entity_id, limit=limit, offset=offset)))


@router.get("/claims/{claim_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def claim(
    claim_id: ClaimId, report_id: Annotated[Optional[str], Query(min_length=3, max_length=120)] = None,
    current_user: dict = Depends(get_current_user),
):
    return _ok("Claim", _run("Claim lookup", lambda: _service().get_claim(current_user["user_id"], claim_id, report_id=report_id)))


@router.get("/evidence/{evidence_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def evidence(evidence_id: EvidenceId, current_user: dict = Depends(get_current_user)):
    return _ok("Evidence", _run("Evidence lookup", lambda: _service().get_evidence(current_user["user_id"], evidence_id)))


@router.get("/sources", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def sources(
    query: QueryText = "", freshness: Annotated[Optional[str], Query(pattern="^(fresh|aging|old|unknown)$")] = None,
    limit: Limit = 25, offset: Offset = 0, current_user: dict = Depends(get_current_user),
):
    return _ok("Sources", _run("Listing sources", lambda: _service().list_sources(
        current_user["user_id"], query=query, freshness=freshness, limit=limit, offset=offset)))


@router.get("/sources/{source_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def source(source_id: SourceId, current_user: dict = Depends(get_current_user)):
    return _ok("Source", _run("Source lookup", lambda: _service().get_source(current_user["user_id"], source_id)))


@router.get("/graph", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def graph(
    focus: Annotated[Optional[str], Query(pattern=_ID_PATTERN)] = None,
    depth: Annotated[int, Query(ge=1, le=3)] = 1, limit: Annotated[int, Query(ge=5, le=300)] = 80,
    current_user: dict = Depends(get_current_user),
):
    return _ok("Knowledge graph", _run("Knowledge graph", lambda: _service().graph(
        current_user["user_id"], focus=focus, depth=depth, limit=limit)))


@router.get("/changes", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
async def changes(
    base: Annotated[str, Query(min_length=3, max_length=120)], target: Annotated[str, Query(min_length=3, max_length=120)],
    current_user: dict = Depends(get_current_user),
):
    """What changed between two investigations: new, strengthened, weakened,
    contradicted and stale claims/evidence. 409 if either side's knowledge
    failed to persist (or was never recorded)."""
    return _ok("What changed", _run("What changed", lambda: _service().what_changed(current_user["user_id"], base, target)))
