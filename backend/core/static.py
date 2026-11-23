"""Static-file serving for the bundled frontend (mounted at "/" by deploy/space_app.py)."""
from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

# Files that change between deploys. Served with "no-cache": the browser keeps a copy but
# revalidates it on every load (cheap 304 via ETag), so a new deploy shows up on a normal reload.
_REVALIDATE = {".html", ".js", ".css", ".json"}


class FrontendFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        suffix = Path(str(full_path)).suffix.lower()
        response.headers["Cache-Control"] = "no-cache" if suffix in _REVALIDATE else "public, max-age=86400"
        return response
