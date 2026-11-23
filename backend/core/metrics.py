"""In-process request metrics: counts, errors and latency per route template.

Deliberately dependency-free and per process (like the rate limiter), so it fits the
single-instance deployment. It answers "is the service healthy right now?" without a
metrics stack; a Prometheus or Datadog exporter can read ``snapshot()`` later.
Route *templates* (``/api/notes/{report_id}``) are recorded, never raw paths, so
identifiers and query strings cannot leak into metrics or inflate cardinality.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_MAX_ROUTES = 300  # hard bound on distinct keys, whatever clients request
_LATENCY_SAMPLES = 200  # recent samples kept per route for percentiles

_lock = threading.Lock()
_started = time.time()
_routes: dict[str, dict[str, Any]] = {}


def record(method: str, route: str, status: int, seconds: float) -> None:
    key = f"{method} {route}"
    with _lock:
        entry = _routes.get(key)
        if entry is None:
            if len(_routes) >= _MAX_ROUTES:
                key = f"{method} (other)"
                entry = _routes.setdefault(key, _new())
            else:
                entry = _routes[key] = _new()
        entry["count"] += 1
        if status >= 500:
            entry["errors_5xx"] += 1
        elif status >= 400:
            entry["errors_4xx"] += 1
        samples = entry["samples"]
        samples.append(seconds)
        if len(samples) > _LATENCY_SAMPLES:
            del samples[0]


def _new() -> dict[str, Any]:
    return {"count": 0, "errors_4xx": 0, "errors_5xx": 0, "samples": []}


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def snapshot() -> dict[str, Any]:
    with _lock:
        routes = []
        total = e4 = e5 = 0
        for key, entry in _routes.items():
            s = sorted(entry["samples"])
            routes.append({
                "route": key, "count": entry["count"],
                "errors_4xx": entry["errors_4xx"], "errors_5xx": entry["errors_5xx"],
                "p50_ms": None if not s else round(_percentile(s, 0.50) * 1000, 1),
                "p95_ms": None if not s else round(_percentile(s, 0.95) * 1000, 1),
            })
            total += entry["count"]
            e4 += entry["errors_4xx"]
            e5 += entry["errors_5xx"]
    routes.sort(key=lambda r: r["count"], reverse=True)
    return {
        "uptime_seconds": int(time.time() - _started),
        "requests_total": total, "errors_4xx": e4, "errors_5xx": e5,
        "error_rate_5xx": round(e5 / total, 4) if total else 0.0,
        "routes": routes,
        "note": "Per process and reset on restart. Latency percentiles use the most recent samples per route.",
    }


def reset() -> None:
    """Test-only."""
    with _lock:
        _routes.clear()


async def metrics_middleware(request: Any, call_next: Any) -> Any:
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "(unmatched)"
        record(request.method, template, status, time.perf_counter() - started)
