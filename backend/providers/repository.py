"""Owner-scoped persistence for Compare runs and prompt-free provider telemetry.

Every read/write that touches a user's run is filtered by user_id, so one
account can never read, list or delete another's runs. ``provider_call_log``
stores metadata only (no prompts, answers or keys).
"""
from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


class CompareRepository:
    MAX_RUNS_PER_USER = 200

    def __init__(self, db: Any) -> None:
        self.db = db

    def save_run(self, user_id: str, run_id: str, kind: str, prompt: str, evidence_mode: str,
                 model_ids: list[str], payload: dict[str, Any], idempotency_key: str | None) -> None:
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO compare_runs(run_id,user_id,kind,prompt,evidence_mode,model_ids_json,payload_json,idempotency_key,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, user_id, kind, prompt, evidence_mode, json.dumps(model_ids),
                 json.dumps(payload, ensure_ascii=False), idempotency_key, payload.get("created_at") or _iso()))
            # bounded history per account
            conn.execute(
                "DELETE FROM compare_runs WHERE user_id=? AND run_id NOT IN "
                "(SELECT run_id FROM compare_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?)",
                (user_id, user_id, self.MAX_RUNS_PER_USER))
            conn.commit()

    def get_run(self, user_id: str, run_id: str) -> dict[str, Any] | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT payload_json FROM compare_runs WHERE run_id=? AND user_id=?",
                               (run_id, user_id)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_by_idempotency(self, user_id: str, key: str) -> dict[str, Any] | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT payload_json FROM compare_runs WHERE user_id=? AND idempotency_key=?",
                               (user_id, key)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_runs(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                "SELECT run_id,kind,prompt,evidence_mode,model_ids_json,created_at FROM compare_runs "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, max(1, min(limit, 200)))).fetchall()
        return [{"run_id": r["run_id"], "kind": r["kind"], "created_at": r["created_at"], "prompt": r["prompt"][:200],
                 "model_count": len(json.loads(r["model_ids_json"])), "evidence_mode": r["evidence_mode"]} for r in rows]

    def delete_run(self, user_id: str, run_id: str) -> bool:
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM compare_runs WHERE run_id=? AND user_id=?", (run_id, user_id))
            conn.commit()
            return cur.rowcount == 1

    def purge_older_than(self, days: int) -> int:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        with closing(self.db.get_connection()) as conn:
            a = conn.execute("DELETE FROM compare_runs WHERE created_at < ?", (cutoff,)).rowcount
            b = conn.execute("DELETE FROM provider_call_log WHERE created_at < ?", (cutoff,)).rowcount
            conn.commit()
        return a + b

    def log_call(self, *, user_id: str, run_id: str | None, request_id: str, provider: str, model: str, status: str,
                 error_category: str | None, attempts: int, latency_ms: int | None, input_tokens: int | None,
                 output_tokens: int | None, cost_usd: float | None, cost_basis: str | None) -> None:
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                "INSERT INTO provider_call_log(call_id,run_id,user_id,request_id,provider,model,status,error_category,attempts,"
                "latency_ms,input_tokens,output_tokens,cost_usd,cost_basis,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"call_{uuid.uuid4().hex}", run_id, user_id, request_id, provider, model, status, error_category,
                 attempts, latency_ms, input_tokens, output_tokens, cost_usd, cost_basis, _iso()))
            conn.commit()

    def telemetry(self, since_iso: str) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                "SELECT provider,model,COUNT(*) AS calls,SUM(status!='ok') AS failed,SUM(attempts-1) AS retries,"
                "AVG(latency_ms) AS avg_latency_ms,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,"
                "SUM(cost_usd) AS cost_usd,SUM(cost_usd IS NULL) AS unknown_cost_calls "
                "FROM provider_call_log WHERE created_at>=? GROUP BY provider,model ORDER BY calls DESC", (since_iso,)).fetchall()
        return [dict(r) for r in rows]
