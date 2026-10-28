"""Batch 3 regression coverage for request correlation and cost semantics."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.core.logging import request_id_ctx
from backend.core.request_context import request_context_middleware
from backend.database.sqlite import SQLiteManager
from backend.ops.repository import OperationalRepository
from backend.routing.service import RoutingService


def test_request_id_is_correlated_to_response_and_invalid_ids_are_replaced():
    app = FastAPI()
    app.middleware("http")(request_context_middleware)

    @app.get("/probe")
    def probe(request: Request):
        return {"request_id": request_id_ctx.get()}

    with TestClient(app) as client:
        valid = client.get("/probe", headers={"X-Request-ID": "client.trace-123"})
        assert valid.status_code == 200
        assert valid.headers["x-request-id"] == "client.trace-123"
        assert valid.json()["request_id"] == "client.trace-123"

        invalid = client.get("/probe", headers={"X-Request-ID": "bad\r\nheader"})
        assert invalid.status_code == 200
        assert invalid.headers["x-request-id"] == invalid.json()["request_id"]
        assert invalid.json()["request_id"] != "bad\r\nheader"
        assert len(invalid.json()["request_id"]) == 32


def test_routing_usage_view_names_usage_cost_estimate_and_no_provider_billed_cost():
    row = {
        "usage_id": 1, "created_at": "2026-10-14T00:00:00+00:00", "run_ref": "ref", "purpose": "chat",
        "policy_id": None, "policy_name": "default", "mode": "explicit", "reason_code": "explicit", "reason": "chosen",
        "requested_model": "model-a", "actual_model": "model-a", "status": "ok", "fallback_used": 0,
        "fallback_reason": None, "attempts_json": "[]", "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": 15, "tokens_reported": 1, "est_cost_usd": 0.0003, "actual_cost_usd": 0.0002,
        "latency_ms": 100, "llm_latency_ms": 90, "error_type": None, "evidence_run_id": None,
    }
    view = RoutingService._usage_view(row)
    assert view["usage_cost_estimate_usd"] == 0.0002
    assert view["provider_reported_cost_usd"] is None
    # Legacy API field remains for backwards compatibility.
    assert view["actual_cost_usd"] == 0.0002


def test_admin_monitoring_does_not_mislabel_usage_estimate_as_provider_reported_cost(tmp_path):
    db = SQLiteManager(str(tmp_path / "runtime.db"))
    db.initialize_database()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO users(user_id,email,name,password_hash,password_salt,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("u1", "u1@example.test", "User", "hash", "salt", "user", now, now),
        )
        conn.execute(
            "INSERT INTO llm_usage(user_id,run_ref,purpose,policy_name,mode,reason_code,reason,requested_model,actual_model,status,fallback_used,attempts_json,est_cost_usd,actual_cost_usd,latency_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("u1", "r1", "chat", "default", "explicit", "explicit", "selected", "model-a", "model-a", "ok", 0, "[]", 0.0003, 0.0002, 10, now),
        )
        conn.commit()
    result = OperationalRepository(db).monitoring("2020-01-01T00:00:00+00:00")
    assert result["reported_cost_usd"] is None
    assert result["usage_cost_estimate_usd"] == 0.0002
    assert result["usage_cost_estimate_calls"] == 1
    assert result["priced_calls"] == 1
