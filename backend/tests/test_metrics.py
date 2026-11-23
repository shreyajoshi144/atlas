from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import metrics
from backend.core.metrics import metrics_middleware


def _app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(metrics_middleware)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")

    return app


def test_routes_are_recorded_by_template_not_raw_path():
    metrics.reset()
    client = TestClient(_app(), raise_server_exceptions=False)
    for i in range(3):
        assert client.get(f"/items/user-{i}-private?token=abc").status_code == 200
    snap = metrics.snapshot()
    keys = [r["route"] for r in snap["routes"]]
    assert keys == ["GET /items/{item_id}"]
    assert snap["routes"][0]["count"] == 3
    blob = str(snap)
    assert "private" not in blob and "token" not in blob


def test_5xx_and_4xx_are_counted_separately():
    metrics.reset()
    client = TestClient(_app(), raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500
    assert client.get("/missing").status_code == 404
    snap = metrics.snapshot()
    assert snap["errors_5xx"] == 1 and snap["errors_4xx"] == 1
    assert snap["error_rate_5xx"] == 0.5


def test_distinct_route_keys_are_bounded():
    metrics.reset()
    for i in range(metrics._MAX_ROUTES + 50):
        metrics.record("GET", f"/r{i}", 200, 0.001)
    assert len(metrics.snapshot()["routes"]) <= metrics._MAX_ROUTES + 1


def test_percentiles_are_reported():
    metrics.reset()
    for ms in (10, 20, 30, 40, 100):
        metrics.record("GET", "/x", 200, ms / 1000)
    row = metrics.snapshot()["routes"][0]
    assert row["p50_ms"] == 30.0 and row["p95_ms"] == 100.0
