"""AtlasBench API (M8): benchmark tasks, agent configurations, reproducible runs, comparison.

All endpoints are authenticated and scoped to the caller. A task, config or run that does
not exist *or belongs to another user* yields the same 404. Executing a run is
rate-limited per user, allowed for one run at a time per account, and idempotent
(a completed run returns its stored result; a failed or interrupted one resumes with the
cases that are still unfinished). Raw error text never leaves the server.
"""
from __future__ import annotations

import threading
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, ValidationError

from backend.auth.dependencies import get_current_user
from backend.auth.rate_limit import rate_limit_by_user
from backend.bench.models import (
    CONFIG_ID_PATTERN, MAX_TASKS_PER_RUN, RUN_ID_PATTERN, RUN_STATUSES, TASK_ID_PATTERN, AgentConfig, BenchDoc, GoldClaim, GoldTrajectory,
)
from backend.bench.repository import BenchConflict, BenchNotFound
from backend.bench.service import BenchService
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.common import BaseSchema
from backend.models.response_models import ApiResponse, ErrorResponse

router = APIRouter()
logger = get_logger(__name__)

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict (state or limit does not allow this action)"},
    429: {"model": ErrorResponse, "description": "Rate limited"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}

TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]
RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]
ConfigId = Annotated[str, Path(pattern=CONFIG_ID_PATTERN)]


class CreateTaskRequest(BaseSchema):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(default="", max_length=500)
    question: str = Field(min_length=10, max_length=300)
    claims: list[GoldClaim] = Field(min_length=1, max_length=5)
    corpus: list[BenchDoc] = Field(min_length=1, max_length=12)
    trajectory: GoldTrajectory = Field(default_factory=GoldTrajectory)
    tags: list[str] = Field(default_factory=list, max_length=5)


class TaskFromClaimRequest(BaseSchema):
    investigation_id: str = Field(min_length=3, max_length=120)
    claim_id: str = Field(pattern=r"^clm_[0-9a-f]{32}$")
    include_challenges: bool = True


class CreateRunRequest(BaseSchema):
    config_id: str = Field(pattern=CONFIG_ID_PATTERN)
    task_ids: Optional[list[Annotated[str, Field(pattern=TASK_ID_PATTERN)]]] = Field(default=None, min_length=1, max_length=MAX_TASKS_PER_RUN)
    suite_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{2,40}$")
    label: str = Field(default="", max_length=120)


class ArbitrateRequest(BaseSchema):
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_ids: list[Annotated[str, Field(pattern=RUN_ID_PATTERN)]] = Field(min_length=2, max_length=4)


def _service() -> BenchService:
    return BenchService(ResearchRepository())


def _run(action: str, fn):
    try:
        return fn()
    except BenchNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except BenchConflict as exc:
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


# One executing benchmark per account (a run spends real model tokens).
_active: set[str] = set()
_active_lock = threading.Lock()


def reset_execution_guard() -> None:
    with _active_lock:
        _active.clear()


# ── overview ─────────────────────────────────────────────────────────────────
@router.get("/overview", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def overview(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="AtlasBench overview", data=_run("Overview", lambda: _service().overview(current_user["user_id"])))


# ── tasks ────────────────────────────────────────────────────────────────────
@router.get("/tasks", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def list_tasks(source: Annotated[Optional[str], Query(pattern="^(builtin|custom)$")] = None,
               tag: Annotated[Optional[str], Query(pattern=r"^[a-z0-9][a-z0-9-]{0,29}$")] = None,
               current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Tasks", data=_run("Listing tasks", lambda: _service().list_tasks(current_user["user_id"], source=source, tag=tag)))


@router.post("/tasks", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def create_task(request: CreateTaskRequest, current_user: dict = Depends(get_current_user)):
    data = _run("Creating task", lambda: _service().create_task(current_user["user_id"], request.model_dump(mode="json")))
    return ApiResponse(success=True, message="Task created", data={"task": data})


@router.post("/tasks/from-claim", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def task_from_claim(request: TaskFromClaimRequest, current_user: dict = Depends(get_current_user)):
    """Freeze a Knowledge Map claim (+ its completed challenges' evidence) into a benchmark task."""
    data, created = _run("Creating task from claim", lambda: _service().create_task_from_claim(
        current_user["user_id"], request.investigation_id, request.claim_id, request.include_challenges))
    return ApiResponse(success=True, message="Task created" if created else "An identical task already exists", data={"task": data, "created": created})


@router.get("/tasks/{task_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def get_task(task_id: TaskId, current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Task", data={"task": _run("Task lookup", lambda: _service().get_task(current_user["user_id"], task_id))})


@router.delete("/tasks/{task_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def delete_task(task_id: TaskId, current_user: dict = Depends(get_current_user)):
    _run("Deleting task", lambda: _service().delete_task(current_user["user_id"], task_id))
    return ApiResponse(success=True, message="Task deleted", data={"task_id": task_id})


# ── configs ──────────────────────────────────────────────────────────────────
@router.get("/configs", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def list_configs(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Configurations", data=_run("Listing configs", lambda: _service().list_configs(current_user["user_id"])))


@router.post("/configs", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def create_config(request: AgentConfig, current_user: dict = Depends(get_current_user)):
    data = _run("Creating config", lambda: _service().create_config(current_user["user_id"], request.model_dump(mode="json")))
    return ApiResponse(success=True, message="Configuration created", data={"config": data})


@router.delete("/configs/{config_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def delete_config(config_id: ConfigId, current_user: dict = Depends(get_current_user)):
    _run("Deleting config", lambda: _service().delete_config(current_user["user_id"], config_id))
    return ApiResponse(success=True, message="Configuration deleted", data={"config_id": config_id})


# ── runs ─────────────────────────────────────────────────────────────────────
@router.post("/runs", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def create_run(request: CreateRunRequest, current_user: dict = Depends(get_current_user)):
    """Create a pending run (nothing executes yet). Provide exactly one of ``task_ids`` / ``suite_id``."""
    data = _run("Creating run", lambda: _service().create_run(
        current_user["user_id"], request.config_id, task_ids=request.task_ids, suite_id=request.suite_id, label=request.label))
    return ApiResponse(success=True, message="Run created", data={"run": data})


@router.post("/arbitrations", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def arbitrate(request: ArbitrateRequest, current_user: dict = Depends(get_current_user)):
    """Select among completed AtlasBench outputs measured on the same frozen evidence."""
    result = _run("Arbitrating benchmark outputs", lambda: _service().arbitrate(
        current_user["user_id"], request.task_id, request.run_ids))
    return ApiResponse(success=True, message="Arbitration recorded", data={"arbitration": result})


@router.get("/arbitrations", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def list_arbitrations(limit: Annotated[int, Query(ge=1, le=100)] = 25,
                      offset: Annotated[int, Query(ge=0, le=100000)] = 0,
                      current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Arbitrations", data=_run("Listing arbitrations", lambda: _service().list_arbitrations(
        current_user["user_id"], limit, offset)))


@router.get("/arbitrations/{arbitration_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def get_arbitration(arbitration_id: Annotated[str, Path(pattern=r"^arb_[0-9a-f]{32}$")],
                    current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Arbitration", data={"arbitration": _run("Arbitration lookup", lambda: _service().get_arbitration(
        current_user["user_id"], arbitration_id))})


@router.get("/runs", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def list_runs(status: Annotated[Optional[str], Query(pattern="^(" + "|".join(RUN_STATUSES) + ")$")] = None,
              config_hash: Annotated[Optional[str], Query(pattern=r"^[0-9a-f]{64}$")] = None,
              fingerprint: Annotated[Optional[str], Query(pattern=r"^[0-9a-f]{64}$")] = None,
              limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=100000)] = 0,
              current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Runs", data=_run("Listing runs", lambda: _service().list_runs(
        current_user["user_id"], status=status, config_hash=config_hash, fingerprint=fingerprint, limit=limit, offset=offset)))


@router.get("/compare", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def compare_runs(run_ids: Annotated[str, Query(min_length=10, max_length=400, description="Comma-separated run ids (2–4)")],
                 current_user: dict = Depends(get_current_user)):
    import re

    ids_ = [r.strip() for r in run_ids.split(",") if r.strip()]
    if any(not re.match(RUN_ID_PATTERN, r) for r in ids_):
        raise HTTPException(status_code=400, detail="run_ids must be comma-separated run ids.")
    return ApiResponse(success=True, message="Comparison", data=_run("Comparing runs", lambda: _service().compare(current_user["user_id"], ids_)))


@router.get("/runs/{run_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def get_run(run_id: RunId, current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Run", data={"run": _run("Run lookup", lambda: _service().get_run(current_user["user_id"], run_id))})


@router.get("/runs/{run_id}/cases/{task_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def inspect_case(run_id: RunId, task_id: TaskId, current_user: dict = Depends(get_current_user)):
    """One task's full result: model output, per-metric detail, every citation checked, tool trajectory, usage."""
    return ApiResponse(success=True, message="Case", data={"case": _run("Case inspection", lambda: _service().inspect_case(current_user["user_id"], run_id, task_id))})


@router.post("/runs/{run_id}/execute", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES,
             dependencies=[Depends(rate_limit_by_user("bench_execute", limit=6, window_seconds=300))])
def execute_run(run_id: RunId, current_user: dict = Depends(get_current_user)):
    """Execute (or resume) a run. ``success`` is true only if the run completed."""
    uid = current_user["user_id"]
    with _active_lock:
        if uid in _active:
            raise HTTPException(status_code=409, detail="A benchmark run is already executing for this account.")
        _active.add(uid)
    try:
        view, ran = _run("Executing run", lambda: _service().execute_run(uid, run_id))
    finally:
        with _active_lock:
            _active.discard(uid)
    ok = view["status"] == "completed"
    message = ("Run completed" if ran else "Run already completed") if ok else "Run did not complete"
    return ApiResponse(success=ok, message=message, data={"run": view, "ran": ran})


@router.post("/runs/{run_id}/reproduce", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def reproduce_run(run_id: RunId, current_user: dict = Depends(get_current_user)):
    """A new pending run over the exact config and task snapshots of this one."""
    return ApiResponse(success=True, message="Reproduction created", data={"run": _run("Reproducing run", lambda: _service().reproduce_run(current_user["user_id"], run_id))})


@router.delete("/runs/{run_id}", response_model=ApiResponse[dict[str, Any]], responses=ERROR_RESPONSES)
def delete_run(run_id: RunId, current_user: dict = Depends(get_current_user)):
    _run("Deleting run", lambda: _service().delete_run(current_user["user_id"], run_id))
    return ApiResponse(success=True, message="Run deleted", data={"run_id": run_id})
