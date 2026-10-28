"""/api/compare — multi-provider model comparison (OpenAI, Anthropic, Gemini, xAI).

Thin HTTP layer over ``backend.providers.service.CompareService``. All authentication
is enforced by main.py's router-level dependency; handlers additionally re-read the
caller from the dependency (never from the request body) and every stored run is
owner-scoped. Provider API keys never appear in any response.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from backend.auth.dependencies import get_current_user, require_admin_user
from backend.auth.rate_limit import rate_limit_by_user
from backend.core.errors import safe_server_error
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.response_models import ApiResponse
from backend.providers.catalog import load_catalog
from backend.providers.client import ProviderClient
from backend.providers.discovery import DiscoveryCache
from backend.providers.evalstore import EvalStore
from backend.providers.evaluation import default_eval_dir
from backend.providers.repository import CompareRepository
from backend.providers.service import CompareError, CompareService, OperationalBudget, RunRequest
from backend.providers.settings import load_settings

router = APIRouter()
logger = get_logger(__name__)

_service: CompareService | None = None
_lock = threading.Lock()


# ───────────────────────── evidence retrieval (same sources as the Playground) ─────────────────────────
async def _evidence(user_id: str, prompt: str, documents: bool, web: bool, top_k: int) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    if documents:
        try:
            from backend.api.routes.chat import _retrieve_context, _vector_memory

            def fetch() -> dict[str, Any]:
                return _retrieve_context(_vector_memory(), "", prompt, [], top_k, user_id)

            found = await asyncio.to_thread(fetch)
            for chunk in found.get("retrieved_chunks", []):
                items.append({"kind": "document", "title": chunk.get("topic") or "Research document",
                              "excerpt": str(chunk.get("content", "")), "report_id": chunk.get("report_id")})
        except Exception as exc:
            logger.warning("Compare document retrieval failed", extra={"error_type": type(exc).__name__})
            warnings.append("Your research library could not be searched for this run.")
    if web:
        try:
            from backend.services.tavily_service import TavilySearchService
            results = await asyncio.to_thread(lambda: TavilySearchService().search(prompt, max_results=5))
            for r in results:
                items.append({"kind": "web", "title": r.title, "url": str(r.url), "domain": r.domain,
                              "excerpt": r.snippet})
        except Exception as exc:
            logger.warning("Compare web search failed", extra={"error_type": type(exc).__name__})
            warnings.append("Web search was unavailable; models ran without web evidence.")
    return items, warnings


def get_service() -> CompareService:
    global _service
    with _lock:
        if _service is None:
            from backend.core.config import get_settings
            app = get_settings()
            ps = load_settings()
            catalog = load_catalog()
            client = ProviderClient(ps)
            db = ResearchRepository().db
            repo = CompareRepository(db)
            try:
                repo.purge_older_than(ps.compare_retention_days)
            except Exception as exc:  # retention must never block startup
                logger.warning("Compare retention purge failed", extra={"error_type": type(exc).__name__})
            _service = CompareService(
                settings=ps, client=client, catalog=catalog, repo=repo,
                budget=OperationalBudget(db, lambda: app.default_daily_llm_call_limit,
                                         lambda: app.default_monthly_llm_spend_cap_usd),
                evidence_fn=_evidence, discovery=DiscoveryCache(client, catalog, ps),
                eval_store=EvalStore(default_eval_dir(ps)))
        return _service


def reset_service_for_tests(service: CompareService | None = None) -> None:
    global _service
    _service = service


def _ok(message: str, data: Any) -> ApiResponse:
    return ApiResponse(success=True, message=message, data=data)


def _raise(exc: CompareError) -> None:
    raise HTTPException(status_code=exc.status, detail=exc.detail)


# ───────────────────────── request models ─────────────────────────
class _Base(BaseModel):
    prompt: str = Field(min_length=3, max_length=50000)        # real length limit comes from settings in the service
    evidence_mode: Literal["none", "atlas", "provider_search"] = "none"
    include_documents: bool = False
    include_web: bool = False
    structured: bool = False
    max_output_tokens: int | None = Field(default=None, ge=64, le=65536)
    consent_external: bool = False
    idempotency_key: str | None = Field(default=None, max_length=80)


class RunBody(_Base):
    models: list[str] = Field(min_length=1, max_length=4)


class AskBody(_Base):
    models: list[str] = Field(min_length=1, max_length=3)


def _to_req(body: _Base, kind: str) -> RunRequest:
    return RunRequest(prompt=body.prompt, models=list(body.models), evidence_mode=body.evidence_mode,  # type: ignore[attr-defined]
                      include_documents=body.include_documents, include_web=body.include_web,
                      structured=body.structured, max_output_tokens=body.max_output_tokens,
                      consent_external=body.consent_external, idempotency_key=body.idempotency_key, kind=kind)


# ───────────────────────── user routes ─────────────────────────
@router.get("/models", response_model=ApiResponse[dict[str, Any]])
async def list_models(current_user: dict = Depends(get_current_user)):
    return _ok("Compare models", get_service().models_payload())


@router.post("/run", response_model=ApiResponse[dict[str, Any]],
             dependencies=[Depends(rate_limit_by_user("compare_run", limit=6, window_seconds=300))])
async def run_compare(body: RunBody, current_user: dict = Depends(get_current_user)):
    try:
        data = await get_service().run(current_user["user_id"], _to_req(body, "compare"))
    except CompareError as exc:
        _raise(exc)
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Model comparison", exc) from exc
    return _ok("Comparison complete", data)


@router.post("/ask", response_model=ApiResponse[dict[str, Any]],
             dependencies=[Depends(rate_limit_by_user("compare_run", limit=6, window_seconds=300))])
async def ask_with_fallback(body: AskBody, current_user: dict = Depends(get_current_user)):
    try:
        data = await get_service().ask(current_user["user_id"], _to_req(body, "ask"))
    except CompareError as exc:
        _raise(exc)
    except HTTPException:
        raise
    except Exception as exc:
        raise safe_server_error(logger, "Model request", exc) from exc
    return _ok("Answer complete", data)


@router.get("/runs", response_model=ApiResponse[dict[str, Any]])
async def list_runs(current_user: dict = Depends(get_current_user)):
    return _ok("Saved runs", {"items": get_service().repo.list_runs(current_user["user_id"])})


@router.get("/runs/{run_id}", response_model=ApiResponse[dict[str, Any]])
async def get_run(run_id: str, current_user: dict = Depends(get_current_user)):
    run = get_service().repo.get_run(current_user["user_id"], run_id)
    if run is None:                      # also what another user's run looks like: never confirm existence
        raise HTTPException(status_code=404, detail="Run not found")
    return _ok("Saved run", run)


@router.delete("/runs/{run_id}", response_model=ApiResponse[dict[str, Any]])
async def delete_run(run_id: str, current_user: dict = Depends(get_current_user)):
    if not get_service().repo.delete_run(current_user["user_id"], run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return _ok("Run deleted", {"run_id": run_id})


@router.get("/evaluations", response_model=ApiResponse[dict[str, Any]])
async def evaluations(current_user: dict = Depends(get_current_user)):
    return _ok("Evaluation evidence", get_service().evaluations_payload())


# ───────────────────────── admin routes ─────────────────────────
@router.get("/admin/metrics")
async def admin_metrics(format: Literal["json", "prometheus"] = Query("json"),
                        admin: dict = Depends(require_admin_user)):
    svc = get_service()
    if format == "prometheus":
        return PlainTextResponse(svc.client.metrics.prometheus(svc.client.circuit_states()))
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return _ok("Provider metrics", {"process": svc.client.metrics.snapshot(), "circuits": svc.client.circuit_states(),
                                    "persisted_last_7_days": svc.repo.telemetry(since)})


@router.get("/admin/health", response_model=ApiResponse[dict[str, Any]])
async def admin_health(admin: dict = Depends(require_admin_user)):
    svc = get_service()
    return _ok("Provider health", {
        "providers": {p: {"configured": svc.s.configured(p), "circuit": svc.client.circuit_state(p),
                          "last_success_epoch": svc.client.last_success.get(p)} for p in svc.client.breakers},
        "discovery": svc.discovery.last_report if svc.discovery else {},
        "catalog_verified_on": svc.catalog.verified_on, "catalog_stale": svc.catalog.is_stale()})


@router.post("/admin/discover", response_model=ApiResponse[dict[str, Any]],
             dependencies=[Depends(rate_limit_by_user("compare_discover", limit=6, window_seconds=300))])
async def admin_discover(admin: dict = Depends(require_admin_user)):
    svc = get_service()
    if not svc.discovery:
        raise HTTPException(status_code=503, detail="Discovery is not available.")
    return _ok("Model discovery complete", await svc.discovery.refresh())
