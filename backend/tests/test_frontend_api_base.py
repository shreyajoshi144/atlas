"""Regression: the frontend must default to its own origin for API calls.

A hard-coded http://localhost:8000/api default is blocked by the page's
`connect-src 'self'` CSP, surfacing as "Could not reach the server".
"""
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[2] / "frontend" / "js"


def test_frontend_api_base_has_no_hardcoded_port():
    for name in ("auth-page.js", "script.js"):
        assert ":8000" not in (JS_DIR / name).read_text(encoding="utf-8"), name
