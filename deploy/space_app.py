"""Single-origin entrypoint for Hugging Face Spaces (or any one-container host).

Serves the existing FastAPI app unchanged (all /api/*, /health, /ready routes)
and, last, mounts the static frontend/ folder at "/". Because the API and UI
share one origin, the frontend's built-in default (same-origin /api) just works
and no CORS configuration is exercised by the browser.

main.py is not modified.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.static import FrontendFiles
from main import app  # noqa: E402  (existing application, routes already registered)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Mounted after every API route, so /api/*, /health, /ready, /docs keep precedence.
app.mount("/", FrontendFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
