"""Routing persistence (SQLite). EVERY statement is scoped by ``user_id``."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.sqlite import SQLiteManager
from backend.routing.models import MAX_POLICIES_PER_USER, MAX_USAGE_ROWS_PER_USER, RoutingPolicy


class RoutingNotFound(LookupError):
    """No such object for this user (or it belongs to someone else)."""


class RoutingConflict(Exception):
    """The request conflicts with current state (duplicate, limit, active policy)."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class RoutingRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── policies ─────────────────────────────────────────────────────────────
    def create_policy(self, user_id: str, policy: RoutingPolicy, now: datetime) -> dict[str, Any]:
        pid = f"pol_{uuid.uuid4().hex}"
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM routing_policies WHERE user_id = ?", (user_id,)).fetchone()[0]
                if n >= MAX_POLICIES_PER_USER:
                    raise RoutingConflict(f"You can keep at most {MAX_POLICIES_PER_USER} routing policies; delete one first.")
                conn.execute("INSERT INTO routing_policies (policy_id,user_id,name,policy_hash,policy_json,created_at) VALUES (?,?,?,?,?,?)",
                             (pid, user_id, policy.name, policy.policy_hash, json.dumps(policy.model_dump(mode="json"), sort_keys=True), _iso(now)))
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise RoutingConflict("You already have a policy with that name.") from exc
            except Exception:
                conn.rollback()
                raise
        return self.get_policy_row(user_id, pid)

    def get_policy_row(self, user_id: str, policy_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM routing_policies WHERE policy_id = ? AND user_id = ?", (policy_id, user_id))
        if not rows:
            raise RoutingNotFound(policy_id)
        return rows[0]

    def list_policy_rows(self, user_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM routing_policies WHERE user_id = ? ORDER BY created_at DESC, rowid DESC", (user_id,))

    def delete_policy(self, user_id: str, policy_id: str) -> None:
        self.get_policy_row(user_id, policy_id)
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM routing_policies WHERE policy_id = ? AND user_id = ? "
                               "AND NOT EXISTS (SELECT 1 FROM routing_active a WHERE a.policy_id = routing_policies.policy_id AND a.user_id = ?)",
                               (policy_id, user_id, user_id))
            conn.commit()
            if cur.rowcount != 1:
                raise RoutingConflict("This policy is active. Deactivate it before deleting.")

    def active_policy_row(self, user_id: str) -> Optional[dict[str, Any]]:
        rows = self._rows("SELECT p.*, a.updated_at AS activated_at FROM routing_active a JOIN routing_policies p ON p.policy_id = a.policy_id "
                          "WHERE a.user_id = ? AND p.user_id = ?", (user_id, user_id))
        return rows[0] if rows else None

    def set_active(self, user_id: str, policy_id: Optional[str], now: datetime) -> None:
        with closing(self.db.get_connection()) as conn:
            if policy_id is None:
                conn.execute("DELETE FROM routing_active WHERE user_id = ?", (user_id,))
            else:
                if conn.execute("SELECT 1 FROM routing_policies WHERE policy_id = ? AND user_id = ?", (policy_id, user_id)).fetchone() is None:
                    raise RoutingNotFound(policy_id)
                conn.execute("INSERT INTO routing_active (user_id, policy_id, updated_at) VALUES (?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET policy_id = excluded.policy_id, updated_at = excluded.updated_at",
                             (user_id, policy_id, _iso(now)))
            conn.commit()

    # ── usage ────────────────────────────────────────────────────────────────
    def insert_usage(self, user_id: str, row: dict[str, Any], now: datetime) -> int:
        cols = ("run_ref", "purpose", "policy_id", "policy_name", "mode", "reason_code", "reason", "requested_model", "actual_model", "status",
                "fallback_used", "fallback_reason", "attempts_json", "prompt_tokens", "completion_tokens", "total_tokens", "tokens_reported",
                "est_cost_usd", "actual_cost_usd", "latency_ms", "llm_latency_ms", "error_type", "evidence_run_id")
        values = [row.get(c) for c in cols]
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute(f"INSERT INTO llm_usage (user_id,{','.join(cols)},created_at) VALUES (?,{','.join('?' * len(cols))},?)",
                               (user_id, *values, _iso(now)))
            uid = cur.lastrowid
            conn.execute("DELETE FROM llm_usage WHERE user_id = ? AND usage_id <= (SELECT usage_id FROM llm_usage WHERE user_id = ? "
                         "ORDER BY usage_id DESC LIMIT 1 OFFSET ?)", (user_id, user_id, MAX_USAGE_ROWS_PER_USER))
            conn.commit()
        return uid

    def list_usage(self, user_id: str, *, model: Optional[str] = None, purpose: Optional[str] = None, status: Optional[str] = None,
                   fallback_only: bool = False, run_ref: Optional[str] = None, limit: int = 25, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        clauses, params = ["user_id = ?"], [user_id]
        if model:
            clauses.append("(actual_model = ? OR requested_model = ?)")
            params += [model, model]
        for col, val in (("purpose", purpose), ("status", status), ("run_ref", run_ref)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        if fallback_only:
            clauses.append("fallback_used = 1")
        where = " AND ".join(clauses)
        total = self._rows(f"SELECT COUNT(*) AS n FROM llm_usage WHERE {where}", tuple(params))[0]["n"]
        rows = self._rows(f"SELECT * FROM llm_usage WHERE {where} ORDER BY usage_id DESC LIMIT ? OFFSET ?",
                          tuple(params) + (max(1, min(int(limit), 100)), max(0, int(offset))))
        return rows, total

    def usage_summary(self, user_id: str, since_iso: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT COALESCE(actual_model, requested_model) AS model, COUNT(*) AS calls, SUM(status = 'failed') AS failed, SUM(fallback_used) AS fallbacks, "
            "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, SUM(total_tokens) AS total_tokens, SUM(tokens_reported) AS calls_with_tokens, "
            "SUM(actual_cost_usd) AS cost_usd, COUNT(actual_cost_usd) AS calls_with_cost, SUM(est_cost_usd) AS est_cost_usd, COUNT(est_cost_usd) AS calls_with_est, "
            "AVG(latency_ms) AS avg_latency_ms, MAX(latency_ms) AS max_latency_ms "
            "FROM llm_usage WHERE user_id = ? AND created_at >= ? GROUP BY COALESCE(actual_model, requested_model) ORDER BY calls DESC, model",
            (user_id, since_iso))
