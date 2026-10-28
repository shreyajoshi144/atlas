"""Minimal stand-ins used ONLY when the real packages are not importable (e.g. a sandbox without FastAPI).

With the real dependencies installed nothing here is used, and the same tests run against real FastAPI objects.
What this lets us test: handler logic, auth dependency wiring, error mapping, ownership scoping.
What it cannot test: FastAPI's own request parsing/serialization.
"""
from __future__ import annotations

import sys
import types


def install() -> bool:
    """Returns True if stubs were installed."""
    try:
        import fastapi  # noqa: F401
        import backend.auth.dependencies  # noqa: F401
        return False
    except Exception:
        pass

    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail=None, headers=None):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class _Dep:
        def __init__(self, dependency=None):
            self.dependency = dependency

    class APIRouter:
        def __init__(self, *a, **k):
            self.routes = []

        def _reg(self, method):
            def deco(path, **kw):
                def wrap(fn):
                    self.routes.append({"method": method, "path": path, "fn": fn, "dependencies": kw.get("dependencies", [])})
                    return fn
                return wrap
            return deco

        def __getattr__(self, name):
            if name in ("get", "post", "put", "delete", "patch"):
                return self._reg(name.upper())
            raise AttributeError(name)

    fa.APIRouter, fa.HTTPException, fa.Depends = APIRouter, HTTPException, lambda d=None: _Dep(d)
    fa.Query = lambda default=None, **k: default
    fa.Header = lambda default=None, **k: default
    fa.Request = object
    fa.status = types.SimpleNamespace(HTTP_401_UNAUTHORIZED=401, HTTP_403_FORBIDDEN=403, HTTP_429_TOO_MANY_REQUESTS=429)
    fr = types.ModuleType("fastapi.responses")

    class PlainTextResponse:
        def __init__(self, content, **k):
            self.body = content

    fr.PlainTextResponse = PlainTextResponse
    sys.modules.update({"fastapi": fa, "fastapi.responses": fr})

    async def get_current_user(authorization=None):
        raise HTTPException(401, "Not authenticated.")

    async def require_admin_user(current_user=None):
        raise HTTPException(403, "Administrator access required.")

    dep = types.ModuleType("backend.auth.dependencies")
    dep.get_current_user, dep.require_admin_user = get_current_user, require_admin_user
    rl = types.ModuleType("backend.auth.rate_limit")
    rl.rate_limit_by_user = lambda bucket, limit, window_seconds: (lambda: None)
    auth_pkg = types.ModuleType("backend.auth")
    auth_pkg.__path__ = []
    sys.modules.update({"backend.auth": auth_pkg, "backend.auth.dependencies": dep, "backend.auth.rate_limit": rl})

    try:
        import backend.models.response_models  # noqa: F401
    except Exception:
        from typing import Any, Generic, TypeVar
        from pydantic import BaseModel
        T = TypeVar("T")

        class ApiResponse(BaseModel, Generic[T]):
            success: bool
            message: str
            data: Any = None

        rm = types.ModuleType("backend.models.response_models")
        rm.ApiResponse = ApiResponse
        models_pkg = types.ModuleType("backend.models")
        models_pkg.__path__ = []
        sys.modules.update({"backend.models": models_pkg, "backend.models.response_models": rm})
    return True
