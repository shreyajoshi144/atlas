"""Content-Security-Policy selection for API vs. frontend HTML responses.

The API is JSON-only, so it keeps the fully locked-down policy. The static
frontend (mounted at "/" by deploy/space_app.py) needs a policy that lets its
own CSS/JS, Google Fonts and data: images load. Inline <script> blocks are
allowed by SHA-256 hash (computed from the files on disk), never by a blanket
'unsafe-inline' for script-src, and there is no 'unsafe-eval'.
"""
from __future__ import annotations

import base64
import hashlib
import re
from functools import lru_cache
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

API_CSP = "default-src 'none'; frame-ancestors 'none'"

# Pages whose script.js-generated markup and markup itself use inline on*= handlers.
_INLINE_HANDLER_PAGES = {"/app.html"}

_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


@lru_cache(maxsize=None)
def _inline_script_hashes(page: str) -> tuple[str, ...]:
    path = FRONTEND_DIR / page.lstrip("/")
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    hashes = []
    for body in _INLINE_SCRIPT_RE.findall(html):
        if body.strip():
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            hashes.append("'sha256-%s'" % base64.b64encode(digest).decode())
    return tuple(hashes)


def frontend_csp(path: str, frame_ancestors: str = "'none'") -> str:
    page = "/index.html" if path in ("", "/") else path
    script_src = " ".join(("'self'", *_inline_script_hashes(page)))
    directives = [
        "default-src 'none'",
        f"script-src {script_src}",
        "style-src 'self' https://fonts.googleapis.com",
        "style-src-attr 'unsafe-inline'",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
        f"frame-ancestors {frame_ancestors}",
    ]
    if page in _INLINE_HANDLER_PAGES:
        directives.insert(2, "script-src-attr 'unsafe-inline'")
    return "; ".join(directives)


def is_frontend_html(path: str, content_type: str, status_code: int = 200) -> bool:
    """True for HTML documents served by the static mount (not API/docs paths).

    A 304 Not Modified carries no Content-Type, but the browser replaces the cached page's
    headers with the 304's, so it must carry the page's CSP too. Those are classified by path.
    """
    if status_code == 304:
        is_html = path in ("", "/") or path.endswith(".html")
    else:
        is_html = content_type.lower().startswith("text/html")
    if not is_html:
        return False
    return not (path.startswith("/api/") or path in ("/health", "/ready", "/metrics"))
