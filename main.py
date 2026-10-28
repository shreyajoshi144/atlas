from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.routes.analytics import router as analytics_router
from backend.api.routes.admin import router as admin_router
from backend.api.routes.auth import router as auth_router
from backend.api.routes.chat import router as chat_router
from backend.api.routes.playground import router as playground_router
from backend.api.routes.compare import router as compare_router
from backend.api.routes.history import router as history_router
from backend.api.routes.knowledge_base import router as knowledge_base_router
from backend.api.routes.knowledge_map import router as knowledge_map_router
from backend.api.routes.challenges import router as challenges_router
from backend.api.routes.atlasbench import router as atlasbench_router
from backend.api.routes.workflows import router as workflows_router
from backend.api.routes.routing import router as routing_router
from backend.api.routes.notes import router as notes_router
from backend.api.routes.questions import router as questions_router
from backend.api.routes.research import router as research_router
from backend.api.routes.search import router as search_router
from backend.api.routes.settings import router as settings_router
from backend.auth.dependencies import get_current_user
from backend.core.config import get_settings, validate_production_config
from backend.core.logging import configure_logging, get_logger, safe_traceback_frames
from backend.core.request_context import request_context_middleware
from backend.database import ResearchRepository
from backend.ops.repository import UsageLimitExceeded

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    # Fail loudly rather than silently sign every auth token (or hash every
    # password reset lookup) with unsafe production configuration. No-op in
    # development/staging — see validate_production_config for the checks.
    validate_production_config(settings)

    repository = ResearchRepository()
    repository.initialize()

    app.state.settings = settings
    app.state.repository = repository

    logger.info("Atlas AI API started", extra={"app_name": settings.app_name})
    yield
    logger.info("Atlas AI API stopped")


app = FastAPI(
    title="Atlas AI API",
    description="Backend API for Atlas AI Research Intelligence Platform",
    version="1.0.0",
    lifespan=lifespan,
)

# Correlate every HTTP response with structured server logs.
app.middleware("http")(request_context_middleware)

# CORSMiddleware reads allow_origins when it is constructed, so production
# validation must happen before the middleware is created. This keeps the
# middleware configuration and the startup validation on the same Settings
# object and prevents an invalid production CORS configuration from ever
# reaching the middleware. Development retains the existing wildcard behavior.
_settings_at_import = get_settings()
validate_production_config(_settings_at_import)
_cors_allow_origins = (
    _settings_at_import.cors_allowed_origins_list
    if _settings_at_import.app_env == "production"
    else ["*"]
)


# ── Practical request-size ceiling ──────────────────────────────────────────
# Checked against the declared Content-Length before any route or pydantic
# validation runs, as a coarse first line of defense against obviously
# oversized payloads. This only sees a declared Content-Length — a request
# using chunked transfer-encoding without one bypasses this specific check
# and falls through to normal body reading (a known, accepted limitation for
# this phase; see the security report).
@app.middleware("http")
async def limit_request_body_size_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > get_settings().max_request_body_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request body too large."})
    return await call_next(request)


# ── Security headers ─────────────────────────────────────────────────────────
# Applied to every response. CSP is skipped for the interactive API docs
# pages, which load Swagger/ReDoc's UI assets from a CDN and would be broken
# by a strict default-src — everywhere else this is a pure JSON API with no
# first-party HTML/JS to protect beyond the standard clickjacking/MIME-sniffing
# headers, so a strict CSP there costs nothing.
_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path not in _DOCS_PATHS:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    # HSTS only makes sense once the app is actually served over HTTPS —
    # sending it over plain HTTP (e.g. local dev) has no effect but is
    # still misleading, so it's gated on production specifically.
    if get_settings().app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# CORSMiddleware is added LAST so it becomes the OUTERMOST middleware
# (Starlette wraps middleware in the reverse of registration order) — that
# way it still adds CORS headers to responses that short-circuit inside the
# middlewares above (e.g. a 413 from the body-size check), not just to
# responses that make it all the way to a route handler.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    # The frontend authenticates with a bearer token in the Authorization
    # header (see fetch calls in script.js) — it never sends cookies, so
    # credentialed CORS isn't needed. Browsers reject the combination of a
    # wildcard origin with allow_credentials=True anyway, which would break
    # cross-origin requests the moment the frontend is deployed on a
    # different origin than the API. Leaving this False keeps allow_origins=["*"]
    # actually functional in development; production uses an explicit
    # origin list instead of "*" (see CORS_ALLOWED_ORIGINS / validate_production_config).
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global exception handler — records sanitized stack locations only. ─────
@app.exception_handler(UsageLimitExceeded)
async def usage_limit_exception_handler(request: Request, exc: UsageLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Log only the path (never query parameters) and safe exception metadata.
    # Query strings can contain reset tokens, signed URLs, or credentials.
    logger.error(
        "Unhandled exception",
        extra={
            "path": request.url.path,
            "method": request.method,
            "error_type": type(exc).__name__,
            "stack_frames": safe_traceback_frames(exc.__traceback__),
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Please try again."},
    )


# ── Route registration ────────────────────────────────────────────────────────
#
# Frontend base: API_BASE_URL = "http://localhost:8000/api"
#
# POST   /api/auth/register                        → auth (open)
# POST   /api/auth/login                            → auth (open)
# GET    /api/auth/me                                → auth (protected)
# /api/atlasbench/*                                 → AtlasBench (protected, user-scoped)
# /api/workflows/*                                  → Workflow Studio (protected, user-scoped)
# /api/routing/*                                    → Cost Autopilot + model routing (protected, user-scoped)
# /api/compare/*                                    → Multi-provider comparison: OpenAI/Anthropic/Gemini/xAI (protected, user-scoped; /admin/* needs role=admin)
# POST   /api/research/run                          → research (protected)
# POST   /api/chat/research                         → chat (protected)
# POST   /api/search/semantic                       → search (protected)
# GET    /api/history/reports                       → history (protected)
# GET    /api/history/report/{report_id}            → history (protected)
# GET    /api/analytics/overview                    → analytics (protected)
# GET    /api/knowledge-base                        → knowledge_base (protected)
# DELETE /api/knowledge-base/report/{report_id}      → knowledge_base (protected)
# GET    /api/notes/{report_id}                      → notes (protected, owner-only)
# PUT    /api/notes/{report_id}                      → notes (protected, owner-only)
# DELETE /api/notes/{report_id}                      → notes (protected, owner-only)
#
_auth_dep = [Depends(get_current_user)]

app.include_router(auth_router,           prefix="/api/auth",          tags=["auth"])
app.include_router(research_router,       prefix="/api/research",      tags=["research"],       dependencies=_auth_dep)
app.include_router(chat_router,           prefix="/api/chat",          tags=["chat"],           dependencies=_auth_dep)
app.include_router(playground_router,    prefix="/api/playground",     tags=["playground"],     dependencies=_auth_dep)
app.include_router(compare_router,       prefix="/api/compare",        tags=["compare"],        dependencies=_auth_dep)
app.include_router(search_router,         prefix="/api/search",        tags=["search"],         dependencies=_auth_dep)
app.include_router(history_router,        prefix="/api/history",       tags=["history"],        dependencies=_auth_dep)
app.include_router(analytics_router,      prefix="/api/analytics",     tags=["analytics"],      dependencies=_auth_dep)
app.include_router(admin_router,          prefix="/api/admin",         tags=["admin"])
app.include_router(knowledge_base_router, prefix="/api",               tags=["knowledge-base"], dependencies=_auth_dep)
app.include_router(knowledge_map_router,  prefix="/api/knowledge-map", tags=["knowledge-map"],  dependencies=_auth_dep)
app.include_router(challenges_router,     prefix="/api/challenges",     tags=["challenges"],     dependencies=_auth_dep)
app.include_router(atlasbench_router,     prefix="/api/atlasbench",     tags=["atlasbench"],     dependencies=_auth_dep)
app.include_router(workflows_router,       prefix="/api/workflows",      tags=["workflows"],      dependencies=_auth_dep)
app.include_router(routing_router,        prefix="/api/routing",        tags=["routing"],        dependencies=_auth_dep)
app.include_router(notes_router,          prefix="/api/notes",         tags=["notes"],          dependencies=_auth_dep)
app.include_router(questions_router,      prefix="/api/questions",     tags=["questions"],      dependencies=_auth_dep)
app.include_router(settings_router,       prefix="/api/settings",      tags=["settings"],       dependencies=_auth_dep)


@app.get("/health")
async def health_check() -> dict[str, object]:
    # Reaching this handler at all already proves the backend is reachable
    # (an unreachable backend never gets a response, which the frontend
    # distinguishes as "Offline" — see checkHealth()/setHealth() in
    # script.js). What varies is whether the embedding backend
    # (sentence-transformers) is actually loaded: if not, chat/Discover
    # silently lose semantic context even though the API is otherwise up,
    # which is exactly the "Degraded" case the frontend now reports
    # instead of misleadingly showing "System Offline".
    from backend.memory.embedding_service import get_status_embedding_service

    embedding_service = get_status_embedding_service()
    embeddings_available = embedding_service.is_available
    return {
        "status": "ok" if embeddings_available else "degraded",
        "embeddings_available": embeddings_available,
        "embeddings_mode": embedding_service.mode,
    }


@app.get("/ready")
async def readiness_check() -> dict[str, str]:
    """Readiness probe: confirm configuration loads and the persistent DB responds."""
    from fastapi import HTTPException

    try:
        with ResearchRepository().db.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ready"}
    except Exception as exc:
        logger.warning("Readiness check failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=503, detail="Service is not ready.") from exc
