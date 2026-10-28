"""AtlasBench persistence (SQLite). EVERY statement is scoped by ``user_id``.

Run state machine (conditional UPDATEs, never read-then-write):

    pending ──begin──▶ running ──finish──▶ completed (summary set)
       ▲                  │
       └──── failed ◀─────┘   (re-execute: failed ──begin──▶ running, bounded by MAX_RUN_ATTEMPTS)

A ``running`` run whose heartbeat (``updated_at``, bumped after every case) is older
than the lease is considered abandoned and may be re-begun. Cases are persisted one at
a time, so an interrupted run resumes where it stopped instead of starting over.
Case writes only apply to the attempt that currently holds the run's lease.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.bench.models import (
    MAX_CASE_ATTEMPTS, MAX_CUSTOM_CONFIGS_PER_USER, MAX_CUSTOM_TASKS_PER_USER, MAX_RUN_ATTEMPTS, MAX_RUNS_PER_USER,
    RUN_LEASE_SECONDS, RUN_STATUSES, AgentConfig, TaskDefinition,
)
from backend.database.sqlite import SQLiteManager


class BenchNotFound(LookupError):
    """No such object for this user (or it belongs to someone else)."""


class BenchConflict(Exception):
    """The request conflicts with current state (duplicate, wrong state, limit reached)."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class BenchRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── tasks ────────────────────────────────────────────────────────────────
    def create_task(self, user_id: str, task: TaskDefinition, source_kind: str, now: datetime) -> dict[str, Any]:
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM bench_tasks WHERE user_id = ?", (user_id,)).fetchone()[0]
                if n >= MAX_CUSTOM_TASKS_PER_USER:
                    raise BenchConflict(f"You can keep at most {MAX_CUSTOM_TASKS_PER_USER} custom tasks; delete one first.")
                conn.execute(
                    "INSERT INTO bench_tasks (task_id,user_id,title,task_hash,gold_quality,source_kind,task_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (task.task_id, user_id, task.title, task.task_hash, task.gold_quality, source_kind,
                     json.dumps(task.model_dump(mode="json"), sort_keys=True), _iso(now)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_task_row(user_id, task.task_id)

    def get_task_row(self, user_id: str, task_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM bench_tasks WHERE task_id = ? AND user_id = ?", (task_id, user_id))
        if not rows:
            raise BenchNotFound(task_id)
        return rows[0]

    def list_task_rows(self, user_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM bench_tasks WHERE user_id = ? ORDER BY created_at DESC, rowid DESC", (user_id,))

    def delete_task(self, user_id: str, task_id: str) -> None:
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM bench_tasks WHERE task_id = ? AND user_id = ?", (task_id, user_id))
            conn.commit()
            if cur.rowcount != 1:
                raise BenchNotFound(task_id)

    # ── configs ──────────────────────────────────────────────────────────────
    def create_config(self, user_id: str, config: AgentConfig, now: datetime) -> dict[str, Any]:
        cid = new_id("cfg")
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM bench_configs WHERE user_id = ?", (user_id,)).fetchone()[0]
                if n >= MAX_CUSTOM_CONFIGS_PER_USER:
                    raise BenchConflict(f"You can keep at most {MAX_CUSTOM_CONFIGS_PER_USER} custom configurations; delete one first.")
                conn.execute("INSERT INTO bench_configs (config_id,user_id,name,config_hash,config_json,created_at) VALUES (?,?,?,?,?,?)",
                             (cid, user_id, config.name, config.config_hash, json.dumps(config.model_dump(mode="json"), sort_keys=True), _iso(now)))
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise BenchConflict("You already have a configuration with that name.") from exc
            except Exception:
                conn.rollback()
                raise
        return self.get_config_row(user_id, cid)

    def get_config_row(self, user_id: str, config_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM bench_configs WHERE config_id = ? AND user_id = ?", (config_id, user_id))
        if not rows:
            raise BenchNotFound(config_id)
        return rows[0]

    def list_config_rows(self, user_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM bench_configs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC", (user_id,))

    def delete_config(self, user_id: str, config_id: str) -> None:
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM bench_configs WHERE config_id = ? AND user_id = ?", (config_id, user_id))
            conn.commit()
            if cur.rowcount != 1:
                raise BenchNotFound(config_id)

    # ── runs ─────────────────────────────────────────────────────────────────
    def create_run(self, user_id: str, *, label: str, config_id: str, config: AgentConfig, suite_id: Optional[str],
                   suite_hash: Optional[str], fingerprint: str, evaluator_version: str, tasks: list[TaskDefinition],
                   reproduces_run_id: Optional[str], now: datetime) -> dict[str, Any]:
        run_id, ts = new_id("run"), _iso(now)
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM bench_runs WHERE user_id = ?", (user_id,)).fetchone()[0]
                if n >= MAX_RUNS_PER_USER:
                    raise BenchConflict(f"You can keep at most {MAX_RUNS_PER_USER} runs; delete old ones first.")
                conn.execute(
                    "INSERT INTO bench_runs (run_id,user_id,label,status,config_id,config_name,config_hash,config_json,suite_id,suite_hash,"
                    "fingerprint,evaluator_version,reproduces_run_id,case_count,created_at,updated_at) VALUES (?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, user_id, label, config_id, config.name, config.config_hash, json.dumps(config.model_dump(mode="json"), sort_keys=True),
                     suite_id, suite_hash, fingerprint, evaluator_version, reproduces_run_id, len(tasks), ts, ts))
                for i, t in enumerate(tasks):
                    conn.execute("INSERT INTO bench_run_cases (run_id,task_id,ord,task_hash,task_title,task_json) VALUES (?,?,?,?,?,?)",
                                 (run_id, t.task_id, i, t.task_hash, t.title, json.dumps(t.model_dump(mode="json"), sort_keys=True)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_run(user_id, run_id)

    def _terminalize_exhausted_stale_runs(self, user_id: str, now: datetime, run_id: Optional[str] = None) -> None:
        """Reconcile crashed runs whose retry budget is already exhausted.

        This is deliberately terminal-only: stale runs with attempts remaining
        can still be reclaimed by ``begin_run``; this helper never replays work.
        It is invoked by reads as well as execution so polling/history can repair
        an abandoned final attempt instead of showing `running` forever.
        """
        cutoff = _iso(now - timedelta(seconds=RUN_LEASE_SECONDS))
        where_run = " AND run_id = ?" if run_id is not None else ""
        params: tuple[Any, ...] = ( _iso(now), _iso(now), user_id, MAX_RUN_ATTEMPTS, cutoff )
        if run_id is not None:
            params += (run_id,)
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                "UPDATE bench_runs SET status='failed', error_type='interrupted_max_attempts_exhausted', "
                "completed_at=?, updated_at=? WHERE user_id=? AND status='running' AND attempt_count >= ? "
                "AND updated_at < ?" + where_run,
                params,
            )
            conn.commit()

    def get_run(self, user_id: str, run_id: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
        if now is not None:
            self._terminalize_exhausted_stale_runs(user_id, now, run_id)
        rows = self._rows("SELECT * FROM bench_runs WHERE run_id = ? AND user_id = ?", (run_id, user_id))
        if not rows:
            raise BenchNotFound(run_id)
        return rows[0]

    def list_runs(self, user_id: str, *, status: Optional[str] = None, config_hash: Optional[str] = None,
                  fingerprint: Optional[str] = None, limit: int = 25, offset: int = 0,
                  now: Optional[datetime] = None) -> tuple[list[dict[str, Any]], int]:
        if now is not None:
            self._terminalize_exhausted_stale_runs(user_id, now)
        clauses, params = ["user_id = ?"], [user_id]
        for col, val in (("status", status), ("config_hash", config_hash), ("fingerprint", fingerprint)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = " AND ".join(clauses)
        total = self._rows(f"SELECT COUNT(*) AS n FROM bench_runs WHERE {where}", tuple(params))[0]["n"]
        rows = self._rows(f"SELECT * FROM bench_runs WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                          tuple(params) + (max(1, min(int(limit), 100)), max(0, int(offset))))
        return rows, total

    def delete_run(self, user_id: str, run_id: str) -> None:
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM bench_runs WHERE run_id = ? AND user_id = ? AND status <> 'running'", (run_id, user_id))
            conn.commit()
            if cur.rowcount == 1:
                return
        row = self.get_run(user_id, run_id)  # BenchNotFound if absent
        raise BenchConflict("A running run cannot be deleted." if row["status"] == "running" else "Could not delete the run.")

    def begin_run(self, user_id: str, run_id: str, now: datetime) -> tuple[dict[str, Any], int]:
        ts, cutoff = _iso(now), _iso(now - timedelta(seconds=RUN_LEASE_SECONDS))
        with closing(self.db.get_connection()) as conn:
            # A process can die during the final permitted attempt. Such a row
            # cannot be reclaimed by the UPDATE below (attempt_count is already
            # at the cap), and without terminalizing it would remain `running`
            # forever. Mark only expired, exhausted leases as failed; live leases
            # are untouched and stale runs with attempts left remain resumable.
            conn.execute(
                "UPDATE bench_runs SET status='failed', error_type='interrupted_max_attempts_exhausted', "
                "completed_at=?, updated_at=? WHERE run_id=? AND user_id=? AND status='running' "
                "AND attempt_count >= ? AND updated_at < ?",
                (ts, ts, run_id, user_id, MAX_RUN_ATTEMPTS, cutoff),
            )
            # Commit terminalization independently: the subsequent claim may
            # correctly fail because the exhausted run is no longer claimable.
            conn.commit()
            cur = conn.execute(
                "UPDATE bench_runs SET status='running', started_at=COALESCE(started_at, ?), updated_at=?, attempt_count=attempt_count+1, error_type=NULL "
                "WHERE run_id=? AND user_id=? AND attempt_count < ? AND (status IN ('pending','failed') OR (status='running' AND updated_at < ?))",
                (ts, ts, run_id, user_id, MAX_RUN_ATTEMPTS, cutoff))
            if cur.rowcount != 1:
                conn.rollback()
                row = conn.execute("SELECT status, attempt_count FROM bench_runs WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()
                if row is None:
                    raise BenchNotFound(run_id)
                if row["status"] == "completed":
                    raise BenchConflict("completed")
                if row["status"] == "running":
                    raise BenchConflict("This run is already executing.")
                raise BenchConflict(f"This run failed {row['attempt_count']} times; create a new run to try again.")
            attempt = conn.execute("SELECT attempt_count FROM bench_runs WHERE run_id=?", (run_id,)).fetchone()[0]
            conn.commit()
        return self.get_run(user_id, run_id, now=now), attempt

    def pending_cases(self, user_id: str, run_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT c.* FROM bench_run_cases c JOIN bench_runs r ON r.run_id = c.run_id WHERE c.run_id = ? AND r.user_id = ? "
            "AND (c.status = 'pending' OR (c.status = 'failed' AND c.attempt_count < ?)) ORDER BY c.ord",
            (run_id, user_id, MAX_CASE_ATTEMPTS))

    def cases(self, user_id: str, run_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT c.* FROM bench_run_cases c JOIN bench_runs r ON r.run_id = c.run_id "
                          "WHERE c.run_id = ? AND r.user_id = ? ORDER BY c.ord", (run_id, user_id))

    def get_case(self, user_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT c.* FROM bench_run_cases c JOIN bench_runs r ON r.run_id = c.run_id "
                          "WHERE c.run_id = ? AND c.task_id = ? AND r.user_id = ?", (run_id, task_id, user_id))
        if not rows:
            raise BenchNotFound(f"{run_id}/{task_id}")
        return rows[0]

    def record_case(self, user_id: str, run_id: str, task_id: str, run_attempt: int, *, status: str, composite: Optional[float],
                    scores: dict, detail: dict, output: dict, trace: list, raw_llm: list, usage: dict, latency: dict,
                    error_type: Optional[str], started_at: datetime, now: datetime) -> None:
        if status not in ("completed", "failed"):
            raise ValueError(f"invalid case status: {status!r}")
        ts = _iso(now)
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute(
                "UPDATE bench_run_cases SET status=?, attempt_count=attempt_count+1, composite=?, scores_json=?, detail_json=?, output_json=?, "
                "trace_json=?, raw_llm_json=?, prompt_tokens=?, completion_tokens=?, total_tokens=?, cost_usd=?, latency_ms=?, llm_latency_ms=?, "
                "error_type=?, started_at=?, finished_at=? "
                "WHERE run_id=? AND task_id=? AND EXISTS (SELECT 1 FROM bench_runs r WHERE r.run_id=bench_run_cases.run_id AND r.user_id=? "
                "AND r.status='running' AND r.attempt_count=?)",
                (status, composite, json.dumps(scores, sort_keys=True), json.dumps(detail, sort_keys=True), json.dumps(output, sort_keys=True),
                 json.dumps(trace), json.dumps(raw_llm), usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens"),
                 usage.get("cost_usd"), int(latency.get("total_ms", 0)), int(latency.get("llm_ms", 0)), error_type, _iso(started_at), ts,
                 run_id, task_id, user_id, run_attempt))
            if cur.rowcount != 1:
                conn.rollback()
                raise BenchConflict("This attempt no longer holds the run (it was superseded).")
            conn.execute("UPDATE bench_runs SET updated_at=? WHERE run_id=? AND user_id=?", (ts, run_id, user_id))  # heartbeat
            conn.commit()

    def finish_run(self, user_id: str, run_id: str, attempt: int, *, summary: Optional[dict], error_type: Optional[str], now: datetime) -> bool:
        ts = _iso(now)
        status = "completed" if summary is not None else "failed"
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute(
                "UPDATE bench_runs SET status=?, summary_json=?, error_type=?, completed_at=?, updated_at=? "
                "WHERE run_id=? AND user_id=? AND status='running' AND attempt_count=?",
                (status, json.dumps(summary, sort_keys=True) if summary is not None else None, error_type,
                 ts if summary is not None else None, ts, run_id, user_id, attempt))
            conn.commit()
            return cur.rowcount == 1

    def create_arbitration(self, user_id: str, task_id: str, task_hash: str, run_ids: list[str],
                           selected_run_id: str, decision: dict[str, Any], now: datetime) -> dict[str, Any]:
        arbitration_id = new_id("arb")
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                "INSERT INTO bench_arbitrations (arbitration_id,user_id,task_id,task_hash,candidate_run_ids_json,selected_run_id,decision_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (arbitration_id, user_id, task_id, task_hash, json.dumps(run_ids), selected_run_id,
                 json.dumps(decision, sort_keys=True), _iso(now)),
            )
            conn.commit()
        return self.get_arbitration(user_id, arbitration_id)

    def get_arbitration(self, user_id: str, arbitration_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM bench_arbitrations WHERE arbitration_id=? AND user_id=?", (arbitration_id, user_id))
        if not rows:
            raise BenchNotFound(arbitration_id)
        row = rows[0]
        row["candidate_run_ids"] = json.loads(row.pop("candidate_run_ids_json"))
        row["decision"] = json.loads(row.pop("decision_json"))
        if row.get("selected_run_id") == "" and row["decision"].get("decision") == "abstained":
            row["selected_run_id"] = None
        return row

    def list_arbitrations(self, user_id: str, limit: int = 25, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        rows = self._rows("SELECT arbitration_id FROM bench_arbitrations WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                          (user_id, max(1, min(int(limit), 100)), max(0, int(offset))))
        total = self._rows("SELECT COUNT(*) AS n FROM bench_arbitrations WHERE user_id=?", (user_id,))[0]["n"]
        return [self.get_arbitration(user_id, r["arbitration_id"]) for r in rows], total

    def integrity_problems(self, user_id: Optional[str] = None) -> list[str]:
        scope, params = (" AND r.user_id = ?", (user_id,)) if user_id else ("", ())
        checks = {
            "invalid run status": "SELECT r.run_id FROM bench_runs r WHERE r.status NOT IN (%s)" % ",".join(f"'{s}'" for s in RUN_STATUSES),
            "completed run with an unfinished case": (
                "SELECT r.run_id FROM bench_runs r WHERE r.status='completed' AND EXISTS "
                "(SELECT 1 FROM bench_run_cases c WHERE c.run_id=r.run_id AND c.status='pending')"),
            "completed case without scores": (
                "SELECT r.run_id FROM bench_runs r JOIN bench_run_cases c ON c.run_id=r.run_id WHERE c.status='completed' AND c.scores_json IS NULL"),
            "case count mismatch": (
                "SELECT r.run_id FROM bench_runs r WHERE r.case_count <> (SELECT COUNT(*) FROM bench_run_cases c WHERE c.run_id=r.run_id)"),
        }
        return [f"{name}: {list(row.values())[0]}" for name, sql in checks.items() for row in self._rows(sql + scope, params)]
