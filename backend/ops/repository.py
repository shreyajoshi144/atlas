from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database.sqlite import SQLiteManager


class UsageLimitExceeded(RuntimeError):
    """Raised before a model call when an account has reached its configured budget."""


def estimate_budget_cost(model: str, prompt_tokens: int, completion_tokens: int, catalog: dict[str, Any]) -> float | None:
    """Unrounded conservative estimate; round only for display, never for cap enforcement."""
    entry = catalog.get(model) or {}
    price_in, price_out = entry.get("price_in"), entry.get("price_out")
    if price_in is None or price_out is None:
        return None
    return (max(0, prompt_tokens) * float(price_in) + max(0, completion_tokens) * float(price_out)) / 1_000_000


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class OperationalRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    @staticmethod
    def _reconcile_expired(conn: Any, now: datetime) -> None:
        # A timed-out process may have completed provider work before it died.
        # Retain the full estimate and expose the outcome as unresolved failure.
        conn.execute("UPDATE llm_budget_events SET status='completed',result_status='failed',error_type='ReservationExpired' "
                     "WHERE status='reserved' AND expires_at < ?", (_iso(now),))

    def effective_limits(self, user_id: str, default_calls: int, default_spend: float | None) -> dict[str, Any]:
        with closing(self.db.get_connection()) as conn:
            if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
                raise LookupError("User not found")
            row = conn.execute("SELECT daily_call_limit,monthly_spend_cap_usd FROM user_usage_limits WHERE user_id=?", (user_id,)).fetchone()
        return {
            "daily_call_limit": row["daily_call_limit"] if row and row["daily_call_limit"] is not None else default_calls,
            "monthly_spend_cap_usd": row["monthly_spend_cap_usd"] if row and row["monthly_spend_cap_usd"] is not None else default_spend,
            "overrides": dict(row) if row else {"daily_call_limit": None, "monthly_spend_cap_usd": None},
        }

    def reserve_call(self, user_id: str, purpose: str, model: str, estimate: float | None, *,
                     default_calls: int, default_spend: float | None, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        event_id = f"bud_{uuid.uuid4().hex}"
        # Keep the account lookup, current limits, spend/count checks, and
        # reservation inside one write transaction. Reading overrides before
        # BEGIN IMMEDIATE allowed an administrator's concurrent limit update
        # to race a reservation and apply stale limits to a new model call.
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
                raise LookupError("User not found")
            limit_row = conn.execute(
                "SELECT daily_call_limit,monthly_spend_cap_usd FROM user_usage_limits WHERE user_id=?",
                (user_id,),
            ).fetchone()
            call_cap = (limit_row["daily_call_limit"] if limit_row and limit_row["daily_call_limit"] is not None
                        else default_calls)
            spend_cap = (limit_row["monthly_spend_cap_usd"] if limit_row and limit_row["monthly_spend_cap_usd"] is not None
                         else default_spend)
            if spend_cap is not None and estimate is None:
                raise UsageLimitExceeded("Model call blocked because its cost cannot be safely estimated under the active spend cap.")
            # The numeric field is a compatibility placeholder when pricing is
            # unknown; cost_known ensures it is never interpreted as a free call.
            amount = max(0.0, float(estimate)) if estimate is not None else 0.0
            self._reconcile_expired(conn, now)
            daily_count = conn.execute("SELECT COUNT(*) FROM llm_budget_events WHERE user_id=? AND created_at>=?",
                                       (user_id, _iso(day_start))).fetchone()[0]
            unknown_costs = conn.execute(
                "SELECT COUNT(*) FROM llm_budget_events WHERE user_id=? AND created_at>=? AND cost_known=0",
                (user_id, _iso(month_start))).fetchone()[0]
            if spend_cap is not None and unknown_costs:
                conn.commit()
                raise UsageLimitExceeded("Monthly spend cap cannot be safely enforced because earlier model call costs are unknown.")
            monthly_spend = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN actual_cost_usd IS NULL THEN estimated_cost_usd ELSE MAX(actual_cost_usd,estimated_cost_usd) END),0) "
                "FROM llm_budget_events WHERE user_id=? AND created_at>=?", (user_id, _iso(month_start))).fetchone()[0]
            if call_cap is not None and daily_count >= int(call_cap):
                conn.commit()
                raise UsageLimitExceeded("Daily model call limit reached.")
            if spend_cap is not None and float(monthly_spend) + amount > float(spend_cap) + 1e-12:
                conn.commit()
                raise UsageLimitExceeded("Monthly model spend cap would be exceeded.")
            conn.execute("INSERT INTO llm_budget_events(event_id,user_id,purpose,model,estimated_cost_usd,reserved_cost_usd,cost_known,status,created_at,expires_at) "
                         "VALUES(?,?,?,?,?,?,?,'reserved',?,?)",
                         (event_id, user_id, purpose, model, amount, amount, int(estimate is not None), _iso(now), _iso(now + timedelta(minutes=30))))
            conn.commit()
        return event_id

    def finish_call(self, event_id: str, *, actual_cost: float | None, status: str, latency_ms: int,
                    actual_model: str | None = None, prompt_tokens: int | None = None, completion_tokens: int | None = None,
                    error_type: str | None = None,
                    estimated_cost: float | None = None,
                    now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with closing(self.db.get_connection()) as conn:
            conn.execute("UPDATE llm_budget_events SET actual_cost_usd=?,estimated_cost_usd=COALESCE(?,estimated_cost_usd),cost_known=CASE WHEN ? IS NOT NULL THEN 1 ELSE cost_known END,actual_model=?,prompt_tokens=?,completion_tokens=?,result_status=?,error_type=?,latency_ms=?,status='completed',expires_at=NULL "
                         "WHERE event_id=? AND (status='reserved' OR (status='completed' AND error_type='ReservationExpired'))",
                         (actual_cost, estimated_cost, estimated_cost, actual_model, prompt_tokens, completion_tokens, status, error_type, max(0, int(latency_ms)), event_id))
            conn.commit()

    def set_limits(self, user_id: str, *, daily_call_limit: int | None, monthly_spend_cap_usd: float | None,
                   actor_id: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
                raise LookupError("User not found")
            conn.execute("INSERT INTO user_usage_limits(user_id,daily_call_limit,monthly_spend_cap_usd,updated_by,updated_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET daily_call_limit=excluded.daily_call_limit,monthly_spend_cap_usd=excluded.monthly_spend_cap_usd,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                         (user_id, daily_call_limit, monthly_spend_cap_usd, actor_id, _iso(now)))
            details = json.dumps({"daily_call_limit": daily_call_limit, "monthly_spend_cap_usd": monthly_spend_cap_usd}, sort_keys=True)
            conn.execute("INSERT INTO admin_audit_logs(audit_id,actor_user_id,action,target_user_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
                         (f"aud_{uuid.uuid4().hex}", actor_id, "usage_limits.updated", user_id, details, _iso(now)))
            conn.commit()

    def list_users(self, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        with closing(self.db.get_connection()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            rows = conn.execute("SELECT u.user_id,u.email,u.name,u.role,u.created_at,l.daily_call_limit,l.monthly_spend_cap_usd "
                                "FROM users u LEFT JOIN user_usage_limits l ON l.user_id=u.user_id "
                                "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                                (max(1, min(limit, 200)), max(0, offset))).fetchall()
        return [dict(r) for r in rows], total

    def monitoring(self, since_iso: str) -> dict[str, Any]:
        with closing(self.db.get_connection()) as conn:
            # Surface expired calls on the operational dashboard even if the
            # account has not made another request since the reservation timed out.
            self._reconcile_expired(conn, datetime.now(timezone.utc))
            conn.commit()
            usage = conn.execute(
                "SELECT COUNT(*) AS calls,SUM(status='failed') AS failed,SUM(fallback_used) AS fallbacks,"
                "SUM(prompt_tokens) AS prompt_tokens,SUM(completion_tokens) AS completion_tokens,SUM(total_tokens) AS total_tokens,"
                "SUM(actual_cost_usd) AS usage_cost_estimate_usd,COUNT(actual_cost_usd) AS usage_cost_estimate_calls,AVG(latency_ms) AS avg_latency_ms,MAX(latency_ms) AS max_latency_ms "
                "FROM llm_usage WHERE created_at>=?", (since_iso,)).fetchone()
            budgets = conn.execute(
                "SELECT COUNT(*) AS calls,SUM(result_status='failed') AS failed,SUM(prompt_tokens) AS prompt_tokens,SUM(completion_tokens) AS completion_tokens,"
                "SUM(CASE WHEN actual_cost_usd IS NULL THEN estimated_cost_usd ELSE MAX(actual_cost_usd,estimated_cost_usd) END) AS budget_cost_usd,"
                "SUM(cost_known=0) AS unknown_cost_calls,"
                "AVG(latency_ms) AS avg_latency_ms,MAX(latency_ms) AS max_latency_ms "
                "FROM llm_budget_events WHERE created_at>=?", (since_iso,)).fetchone()
            models = conn.execute(
                "SELECT COALESCE(actual_model,model) AS model,purpose,COUNT(*) AS calls,SUM(result_status='failed') AS failed,"
                "SUM(prompt_tokens+completion_tokens) AS tokens,SUM(CASE WHEN actual_cost_usd IS NULL THEN estimated_cost_usd ELSE MAX(actual_cost_usd,estimated_cost_usd) END) AS budget_cost_usd,"
                "SUM(cost_known=0) AS unknown_cost_calls,"
                "AVG(latency_ms) AS avg_latency_ms,MAX(latency_ms) AS max_latency_ms "
                "FROM llm_budget_events WHERE created_at>=? GROUP BY COALESCE(actual_model,model),purpose ORDER BY calls DESC", (since_iso,)).fetchall()
            failures = conn.execute("SELECT error_type,COUNT(*) AS count FROM llm_budget_events WHERE created_at>=? AND result_status='failed' GROUP BY error_type ORDER BY count DESC", (since_iso,)).fetchall()
            traces = conn.execute("SELECT event_type,COUNT(*) AS count FROM analytics_events WHERE created_at>=? GROUP BY event_type ORDER BY count DESC", (since_iso,)).fetchall()
        return {"since": since_iso, "calls": int(budgets["calls"] or 0),
                "production_calls": int(usage["calls"] or 0), "failed_calls": int(budgets["failed"] or 0), "fallbacks": int(usage["fallbacks"] or 0),
                "prompt_tokens": budgets["prompt_tokens"], "completion_tokens": budgets["completion_tokens"],
                "total_tokens": (int(budgets["prompt_tokens"] or 0) + int(budgets["completion_tokens"] or 0)),
                # No provider-billed cost endpoint is collected by Atlas today.
                # Keep the legacy field but do not mislabel our usage-derived estimate.
                "reported_cost_usd": None,
                "usage_cost_estimate_usd": usage["usage_cost_estimate_usd"],
                "usage_cost_estimate_calls": int(usage["usage_cost_estimate_calls"] or 0),
                "budget_cost_usd": None if budgets["unknown_cost_calls"] else budgets["budget_cost_usd"],
                "unknown_cost_calls": int(budgets["unknown_cost_calls"] or 0),
                "priced_calls": int(usage["usage_cost_estimate_calls"] or 0), "avg_latency_ms": budgets["avg_latency_ms"], "max_latency_ms": budgets["max_latency_ms"],
                "by_model_and_purpose": [{**dict(r), "budget_cost_usd": None if r["unknown_cost_calls"] else r["budget_cost_usd"]} for r in models], "failures_by_type": [dict(r) for r in failures],
                "trace_events_by_type": [dict(r) for r in traces]}

    def audit_logs(self, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        with closing(self.db.get_connection()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM admin_audit_logs").fetchone()[0]
            rows = conn.execute("SELECT audit_id,actor_user_id,action,target_user_id,details_json,created_at FROM admin_audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                (max(1, min(limit, 200)), max(0, offset))).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            out.append(item)
        return out, total
