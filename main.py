from __future__ import annotations

import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.routes.analytics import router as analytics_router
from backend.api.routes.chat import router as chat_router
from backend.api.routes.history import router as history_router
from backend.api.routes.knowledge_base import router as knowledge_base_router
from backend.api.routes.research import router as research_router
from backend.api.routes.search import router as search_router
from backend.api.routes.settings import router as settings_router
from backend.core.config import get_settings
from backend.core.logging import configure_logging, get_logger
from backend.database import ResearchRepository

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global exception handler — logs the full traceback so errors are visible ──
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(
        "Unhandled exception",
        extra={
            "path": str(request.url),
            "method": request.method,
            "traceback": tb,
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )


# ── Route registration ────────────────────────────────────────────────────────
#
# Frontend base: API_BASE_URL = "http://localhost:8000/api"
#
# POST   /api/research/run                         → research
# POST   /api/chat/research                        → chat
# POST   /api/search/semantic                      → search
# GET    /api/history/reports                      → history
# GET    /api/history/report/{report_id}           → history
# GET    /api/analytics/overview                   → analytics
# GET    /api/knowledge-base                       → knowledge_base
# DELETE /api/knowledge-base/report/{report_id}   → knowledge_base
#
app.include_router(research_router,       prefix="/api/research",      tags=["research"])
app.include_router(chat_router,           prefix="/api/chat",          tags=["chat"])
app.include_router(search_router,         prefix="/api/search",        tags=["search"])
app.include_router(history_router,        prefix="/api/history",       tags=["history"])
app.include_router(analytics_router,      prefix="/api/analytics",     tags=["analytics"])
app.include_router(knowledge_base_router, prefix="/api",               tags=["knowledge-base"])
app.include_router(settings_router,       prefix="/api/settings",      tags=["settings"])


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
