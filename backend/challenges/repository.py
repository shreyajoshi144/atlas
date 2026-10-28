"""Persistence for challenges (SQLite). Every statement is scoped by ``user_id``.

State machine (enforced with conditional UPDATEs, never read-then-write):

    pending ──begin──▶ running ──complete──▶ completed (result set)
       ▲                  │
       └──── failed ◀─────┘  (retry: failed ──begin──▶ running, up to MAX_ATTEMPTS)

A ``running`` challenge older than the lease is treated as abandoned and may be
re-begun. ``complete``/``fail`` only apply to the attempt that holds the lease.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.challenges.models import CHALLENGE_TYPES, MAX_ATTEMPTS, RESULTS, RUNNING_LEASE_SECONDS, STATUSES
from backend.database.sqlite import SQLiteManager

_MAX_ERROR = 500


class ChallengeNotFound(LookupError):
    """No such challenge for this user (or it belongs to someone else)."""


class ChallengeConflict(Exception):
    """The requested transition is not allowed in the challenge's current state."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def new_challenge_id() -> str:
    return "chl_" + uuid.uuid4().hex


class ChallengeRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    # ── reads (user-scoped) ──────────────────────────────────────────────────
    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get(self, user_id: str, challenge_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT * FROM challenges WHERE challenge_id = ? AND user_id = ?", (challenge_id, user_id))
        if not rows:
            raise ChallengeNotFound(challenge_id)
        return rows[0]

    def list(self, user_id: str, *, investigation_id: Optional[str] = None, claim_id: Optional[str] = None,
             status: Optional[str] = None, result: Optional[str] = None, challenge_type: Optional[str] = None,
             limit: int = 25, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        clauses, params = ["user_id = ?"], [user_id]
        for col, val in (("investigation_id", investigation_id), ("claim_id", claim_id), ("status", status),
                         ("result", result), ("challenge_type", challenge_type)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = " AND ".join(clauses)
        total = self._rows(f"SELECT COUNT(*) AS n FROM challenges WHERE {where}", tuple(params))[0]["n"]
        rows = self._rows(
            f"SELECT * FROM challenges WHERE {where} ORDER BY created_at DESC, challenge_id DESC LIMIT ? OFFSET ?",
            tuple(params) + (max(1, min(int(limit), 100)), max(0, int(offset))))
        return rows, total

    def list_for_claim(self, user_id: str, claim_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM challenges WHERE user_id = ? AND claim_id = ? ORDER BY created_at DESC, challenge_id DESC",
                          (user_id, claim_id))

    def attempts(self, user_id: str, challenge_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT a.* FROM challenge_attempts a JOIN challenges c ON c.challenge_id = a.challenge_id "
            "WHERE a.challenge_id = ? AND c.user_id = ? ORDER BY a.attempt_no", (challenge_id, user_id))

    def sources(self, user_id: str, challenge_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT s.* FROM challenge_sources s JOIN challenges c ON c.challenge_id = s.challenge_id "
            "WHERE s.challenge_id = ? AND c.user_id = ? ORDER BY s.query_index, s.source_id", (challenge_id, user_id))

    def evidence(self, user_id: str, challenge_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT e.*, s.url, s.domain, s.title, s.publish_date, s.independent, s.content_hash, s.retrieved_at "
            "FROM challenge_evidence e "
            "JOIN challenge_sources s ON s.challenge_id = e.challenge_id AND s.source_id = e.source_id "
            "JOIN challenges c ON c.challenge_id = e.challenge_id "
            "WHERE e.challenge_id = ? AND c.user_id = ? ORDER BY e.evidence_id", (challenge_id, user_id))

    def counts_by_result(self, user_id: str) -> dict[str, int]:
        rows = self._rows("SELECT COALESCE(result, status) AS k, COUNT(*) AS n FROM challenges WHERE user_id = ? GROUP BY k", (user_id,))
        return {r["k"]: r["n"] for r in rows}

    # ── create (idempotent while a challenge is active) ──────────────────────
    def create(self, user_id: str, investigation_id: str, claim_id: str, challenge_type: str, claim_text: str,
               base: dict[str, Any], now: datetime) -> tuple[dict[str, Any], bool]:
        if challenge_type not in CHALLENGE_TYPES:
            raise ValueError(f"unknown challenge type: {challenge_type!r}")
        active_sql = ("SELECT * FROM challenges WHERE user_id=? AND investigation_id=? AND claim_id=? AND challenge_type=? "
                      "AND status IN ('pending','running')")
        key = (user_id, investigation_id, claim_id, challenge_type)
        with closing(self.db.get_connection()) as conn:
            existing = conn.execute(active_sql, key).fetchone()
            if existing:
                return dict(existing), False
            cid, ts = new_challenge_id(), _iso(now)
            try:
                conn.execute(
                    "INSERT INTO challenges (challenge_id,user_id,investigation_id,claim_id,challenge_type,status,claim_text,base_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                    (cid, user_id, investigation_id, claim_id, challenge_type, claim_text, json.dumps(base, sort_keys=True), ts, ts))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = conn.execute(active_sql, key).fetchone()  # lost a race to another creator
                if existing:
                    return dict(existing), False
                raise
        return self.get(user_id, cid), True

    # ── state transitions ────────────────────────────────────────────────────
    def begin_attempt(self, user_id: str, challenge_id: str, now: datetime) -> tuple[dict[str, Any], int]:
        """Atomically move pending/failed (or an abandoned running) challenge to running."""
        ts, cutoff = _iso(now), _iso(now - timedelta(seconds=RUNNING_LEASE_SECONDS))
        with closing(self.db.get_connection()) as conn:
            try:
                cur = conn.execute(
                    "UPDATE challenges SET status='running', started_at=?, updated_at=?, attempt_count=attempt_count+1, "
                    "error_type=NULL, error_message=NULL "
                    "WHERE challenge_id=? AND user_id=? AND attempt_count < ? "
                    "AND (status IN ('pending','failed') OR (status='running' AND started_at < ?))",
                    (ts, ts, challenge_id, user_id, MAX_ATTEMPTS, cutoff))
            except sqlite3.IntegrityError as exc:  # another active challenge for the same tuple
                conn.rollback()
                raise ChallengeConflict("Another challenge for this claim and type is already active.") from exc
            if cur.rowcount != 1:
                conn.rollback()
                row = conn.execute("SELECT status, attempt_count FROM challenges WHERE challenge_id=? AND user_id=?", (challenge_id, user_id)).fetchone()
                if row is None:
                    raise ChallengeNotFound(challenge_id)
                if row["status"] == "completed":
                    raise ChallengeConflict("completed")
                if row["status"] == "running":
                    raise ChallengeConflict("This challenge is already running.")
                raise ChallengeConflict(f"This challenge failed {row['attempt_count']} times; create a new challenge to try again.")
            attempt_no = conn.execute("SELECT attempt_count FROM challenges WHERE challenge_id=?", (challenge_id,)).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO challenge_attempts (challenge_id,attempt_no,status,started_at) VALUES (?,?,'running',?)",
                         (challenge_id, attempt_no, ts))
            conn.commit()
        return self.get(user_id, challenge_id), attempt_no

    def complete(self, user_id: str, challenge_id: str, attempt_no: int, *, result: str, result_json: dict[str, Any],
                 sources: list[dict], evidence: list[dict], trace: list[dict], now: datetime) -> None:
        if result not in RESULTS:
            raise ValueError(f"invalid result: {result!r}")
        ts = _iso(now)
        with closing(self.db.get_connection()) as conn:
            try:
                cur = conn.execute(
                    "UPDATE challenges SET status='completed', result=?, result_json=?, completed_at=?, updated_at=?, "
                    "error_type=NULL, error_message=NULL "
                    "WHERE challenge_id=? AND user_id=? AND status='running' AND attempt_count=?",
                    (result, json.dumps(result_json, sort_keys=True), ts, ts, challenge_id, user_id, attempt_no))
                if cur.rowcount != 1:
                    raise ChallengeConflict("This attempt no longer holds the challenge (it was superseded).")
                conn.execute("DELETE FROM challenge_evidence WHERE challenge_id=?", (challenge_id,))
                conn.execute("DELETE FROM challenge_sources WHERE challenge_id=?", (challenge_id,))
                for s in sources:
                    conn.execute(
                        "INSERT INTO challenge_sources (challenge_id,source_id,url,canonical_url,domain,title,publish_date,content_hash,scraped,independent,query_index,retrieved_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (challenge_id, s["source_id"], s["url"], s["canonical_url"], s["domain"], s["title"], s["publish_date"],
                         s["content_hash"], s["scraped"], s["independent"], s["query_index"], s["retrieved_at"]))
                for e in evidence:  # FK (challenge_id, source_id) → challenge_sources: no evidence without a retrieved source
                    conn.execute(
                        "INSERT INTO challenge_evidence (challenge_id,evidence_id,source_id,passage_text,stance,strength) VALUES (?,?,?,?,?,?)",
                        (challenge_id, e["evidence_id"], e["source_id"], e["passage_text"], e["stance"], e["strength"]))
                conn.execute("UPDATE challenge_attempts SET status='completed', finished_at=?, trace_json=? WHERE challenge_id=? AND attempt_no=?",
                             (ts, json.dumps(trace), challenge_id, attempt_no))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def fail(self, user_id: str, challenge_id: str, attempt_no: int, *, error_type: str, message: str,
             trace: list[dict], now: datetime) -> bool:
        """Record a failed attempt (best effort — must never mask the original error)."""
        ts = _iso(now)
        try:
            with closing(self.db.get_connection()) as conn:
                cur = conn.execute(
                    "UPDATE challenges SET status='failed', error_type=?, error_message=?, updated_at=? "
                    "WHERE challenge_id=? AND user_id=? AND status='running' AND attempt_count=?",
                    (error_type[:100], message[:_MAX_ERROR], ts, challenge_id, user_id, attempt_no))
                if cur.rowcount == 1:
                    conn.execute("UPDATE challenge_attempts SET status='failed', finished_at=?, error_type=?, trace_json=? "
                                 "WHERE challenge_id=? AND attempt_no=?", (ts, error_type[:100], json.dumps(trace), challenge_id, attempt_no))
                conn.commit()
                return cur.rowcount == 1
        except Exception:
            return False

    def integrity_problems(self, user_id: Optional[str] = None) -> list[str]:
        """Self-check: evidence without a retrieved source, completed-without-result, etc."""
        scope, params = (" AND c.user_id = ?", (user_id,)) if user_id else ("", ())
        checks = {
            "evidence without retrieved source": (
                "SELECT e.evidence_id FROM challenge_evidence e JOIN challenges c ON c.challenge_id=e.challenge_id "
                "WHERE NOT EXISTS (SELECT 1 FROM challenge_sources s WHERE s.challenge_id=e.challenge_id AND s.source_id=e.source_id)"),
            "evidence from an unscraped source": (
                "SELECT e.evidence_id FROM challenge_evidence e JOIN challenges c ON c.challenge_id=e.challenge_id "
                "JOIN challenge_sources s ON s.challenge_id=e.challenge_id AND s.source_id=e.source_id WHERE s.scraped = 0"),
            "completed challenge without result": "SELECT c.challenge_id FROM challenges c WHERE c.status='completed' AND c.result IS NULL",
            "result on a non-completed challenge": "SELECT c.challenge_id FROM challenges c WHERE c.status<>'completed' AND c.result IS NOT NULL",
            "invalid status": "SELECT c.challenge_id FROM challenges c WHERE c.status NOT IN (%s)" % ",".join(f"'{s}'" for s in STATUSES),
        }
        return [f"{name}: {list(r.values())[0]}" for name, sql in checks.items() for r in self._rows(sql + scope, params)]
