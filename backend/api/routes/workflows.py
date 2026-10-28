from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import Field

from backend.auth.dependencies import get_current_user
from backend.database import ResearchRepository
from backend.models.common import BaseSchema
from backend.models.response_models import ApiResponse
from backend.workflows.service import WorkflowService, WorkflowNotFound, WorkflowConflict

router = APIRouter()

class WorkflowDefinitionRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=10)

class WorkflowCreateRequest(WorkflowDefinitionRequest):
    template_id: str | None = None

class WorkflowExecuteRequest(BaseSchema):
    text: str = Field(max_length=10000)


def _service():
    repo = ResearchRepository()
    svc = WorkflowService(repo.db)
    svc.initialize()
    return svc


def _call(fn):
    try:
        return fn()
    except WorkflowNotFound:
        raise HTTPException(status_code=404, detail="Workflow or run not found") from None
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/templates")
def templates(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow templates", data=_service().templates())

@router.get("")
def list_workflows(current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflows", data=_service().list(current_user["user_id"]))

@router.post("")
def create_workflow(request: WorkflowCreateRequest, current_user: dict = Depends(get_current_user)):
    data = _call(lambda: _service().create(current_user["user_id"], request.model_dump(exclude={"template_id"}), request.template_id))
    return ApiResponse(success=True, message="Workflow saved", data={"workflow": data})

@router.get("/{workflow_id}")
def get_workflow(workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow", data={"workflow": _call(lambda: _service().get(current_user["user_id"], workflow_id))})

@router.put("/{workflow_id}")
def update_workflow(request: WorkflowDefinitionRequest, workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow updated", data={"workflow": _call(lambda: _service().update(current_user["user_id"], workflow_id, request.model_dump()))})

@router.post("/{workflow_id}/duplicate")
def duplicate_workflow(workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow duplicated", data={"workflow": _call(lambda: _service().duplicate(current_user["user_id"], workflow_id))})

@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow deleted", data=_call(lambda: _service().delete(current_user["user_id"], workflow_id)))

@router.post("/{workflow_id}/execute")
def execute_workflow(request: WorkflowExecuteRequest, workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), idempotency_key: str = Header(alias="Idempotency-Key"), current_user: dict = Depends(get_current_user)):
    run, created = _call(lambda: _service().execute(current_user["user_id"], workflow_id, request.text, idempotency_key))
    return ApiResponse(success=run["status"] == "completed", message="Workflow executed" if created else "Existing execution returned", data={"run": run, "created": created})

@router.get("/{workflow_id}/runs")
def workflow_history(workflow_id: str = Path(pattern=r"^wf_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow run history", data=_call(lambda: _service().history(current_user["user_id"], workflow_id)))

@router.get("/runs/{run_id}")
def get_workflow_run(run_id: str = Path(pattern=r"^wfr_[0-9a-f]{32}$"), current_user: dict = Depends(get_current_user)):
    return ApiResponse(success=True, message="Workflow run", data={"run": _call(lambda: _service().get_run(current_user["user_id"], run_id))})
