"""Persistence for the knowledge graph (SQLite).

Every read and write is scoped by ``user_id``. A report's contribution is
written in ONE transaction (all-or-nothing) and replaces that report's previous
contribution, so re-running is idempotent. Objects no longer referenced by any
report are garbage-collected so nothing is orphaned.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database.sqlite import SQLiteManager
from backend.knowledge.models import InvestigationKnowledge

_LINK_TABLES = (
    "knowledge_investigation_status",
    "knowledge_claim_evidence",
    "knowledge_claim_entities",
    "knowledge_evidence_entities",
    "knowledge_claim_observations",
    "knowledge_report_sources",
)


STATUS_COMPLETE = "complete"   # whole input persisted
STATUS_PARTIAL = "partial"     # persisted, but some inputs were dropped (see dropped_* counts)
STATUS_FAILED = "failed"       # write failed and rolled back; any graph present is stale/absent
_MAX_ERROR = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_status(conn: sqlite3.Connection, user_id: str, report_id: str, status: str, *,
                   dropped_sources: int = 0, dropped_evidence: int = 0, dropped_claims: int = 0,
                   error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO knowledge_investigation_status "
        "(report_id,user_id,status,dropped_sources,dropped_evidence,dropped_claims,error,updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(report_id) DO UPDATE SET status=excluded.status, dropped_sources=excluded.dropped_sources, "
        "dropped_evidence=excluded.dropped_evidence, dropped_claims=excluded.dropped_claims, error=excluded.error, "
        "updated_at=excluded.updated_at WHERE user_id = excluded.user_id",
        (report_id, user_id, status, dropped_sources, dropped_evidence, dropped_claims, error, _now()))


def gc_orphans(conn: sqlite3.Connection, user_id: str) -> None:
    """Remove this user's objects that no investigation references any more."""
    conn.execute(
        "DELETE FROM knowledge_evidence WHERE user_id = ? AND evidence_id NOT IN "
        "(SELECT evidence_id FROM knowledge_claim_evidence)", (user_id,))
    conn.execute(
        "DELETE FROM knowledge_claims WHERE user_id = ? AND claim_id NOT IN "
        "(SELECT claim_id FROM knowledge_claim_observations)", (user_id,))
    conn.execute(
        "DELETE FROM knowledge_entities WHERE user_id = ? AND entity_id NOT IN "
        "(SELECT entity_id FROM knowledge_claim_entities UNION "
        " SELECT entity_id FROM knowledge_evidence_entities)", (user_id,))
    conn.execute(
        "DELETE FROM knowledge_sources WHERE user_id = ? AND source_id NOT IN "
        "(SELECT source_id FROM knowledge_report_sources UNION "
        " SELECT source_id FROM knowledge_evidence)", (user_id,))


def purge_report_knowledge(conn: sqlite3.Connection, report_id: str) -> None:
    """Delete a report's links and GC what that orphans. Runs on the caller's
    connection so ``delete_report`` stays a single transaction."""
    users = {
        r[0] for t in _LINK_TABLES
        for r in conn.execute(f"SELECT DISTINCT user_id FROM {t} WHERE report_id = ?", (report_id,))
    }
    for table in _LINK_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE report_id = ?", (report_id,))
    for user_id in users:
        gc_orphans(conn, user_id)


class KnowledgeRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    # ── write ────────────────────────────────────────────────────────────────
    def replace_investigation(self, user_id: str, report_id: str, k: InvestigationKnowledge) -> dict[str, int]:
        if not user_id:
            raise ValueError("user_id is required")
        now = _now()
        with closing(self.db.get_connection()) as conn:
            try:
                owner = conn.execute(
                    "SELECT 1 FROM reports r JOIN research_sessions s ON s.session_id = r.session_id "
                    "WHERE r.report_id = ? AND s.user_id = ?", (report_id, user_id)).fetchone()
                if not owner:
                    raise PermissionError("report does not exist or is not owned by this user")

                for table in _LINK_TABLES:
                    if table != "knowledge_investigation_status":  # status is upserted below, never wiped mid-write
                        conn.execute(f"DELETE FROM {table} WHERE report_id = ? AND user_id = ?", (report_id, user_id))

                for s in k.sources:
                    conn.execute(
                        "INSERT INTO knowledge_sources (source_id,user_id,canonical_url,url,domain,title,publish_date,first_seen_at,last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                        "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE title END, "
                        "publish_date=COALESCE(excluded.publish_date, publish_date) WHERE user_id = excluded.user_id",
                        (s.source_id, user_id, s.canonical_url, s.url, s.domain, s.title, s.publish_date, now, now))
                for rs in k.report_sources:
                    conn.execute(
                        "INSERT OR REPLACE INTO knowledge_report_sources (report_id,source_id,user_id,report_source_id,rank_position,content_hash) "
                        "VALUES (?,?,?,?,?,?)",
                        (report_id, rs.source_id, user_id, rs.report_source_id, rs.rank_position, rs.content_hash))
                for e in k.evidence:
                    conn.execute(
                        "INSERT OR IGNORE INTO knowledge_evidence (evidence_id,user_id,source_id,passage_text,created_at) VALUES (?,?,?,?,?)",
                        (e.evidence_id, user_id, e.source_id, e.passage_text, now))
                for c in k.claims:
                    conn.execute(
                        "INSERT OR IGNORE INTO knowledge_claims (claim_id,user_id,claim_text,normalized_text,created_at) VALUES (?,?,?,?,?)",
                        (c.claim_id, user_id, c.claim_text, c.normalized_text, now))
                for o in k.observations:
                    conn.execute(
                        "INSERT OR REPLACE INTO knowledge_claim_observations (report_id,claim_id,user_id,status,confidence) VALUES (?,?,?,?,?)",
                        (report_id, o.claim_id, user_id, o.status, o.confidence))
                for l in k.claim_evidence:
                    conn.execute(
                        "INSERT OR REPLACE INTO knowledge_claim_evidence (report_id,claim_id,evidence_id,user_id,stance,strength) VALUES (?,?,?,?,?,?)",
                        (report_id, l.claim_id, l.evidence_id, user_id, l.stance, l.strength))
                for en in k.entities:
                    conn.execute(
                        "INSERT OR IGNORE INTO knowledge_entities (entity_id,user_id,name,canonical_name,entity_type,created_at) VALUES (?,?,?,?,?,?)",
                        (en.entity_id, user_id, en.name, en.canonical_name, en.entity_type, now))
                for cid, eid in k.claim_entities:
                    conn.execute("INSERT OR IGNORE INTO knowledge_claim_entities (report_id,claim_id,entity_id,user_id) VALUES (?,?,?,?)",
                                 (report_id, cid, eid, user_id))
                for evid, eid in k.evidence_entities:
                    conn.execute("INSERT OR IGNORE INTO knowledge_evidence_entities (report_id,evidence_id,entity_id,user_id) VALUES (?,?,?,?)",
                                 (report_id, evid, eid, user_id))
                gc_orphans(conn, user_id)
                dropped = k.skipped_sources + k.skipped_unresolved_evidence + k.skipped_claims
                status = STATUS_PARTIAL if dropped else STATUS_COMPLETE
                # Same transaction as the graph: graph and its status can never disagree.
                _upsert_status(conn, user_id, report_id, status, dropped_sources=k.skipped_sources,
                               dropped_evidence=k.skipped_unresolved_evidence, dropped_claims=k.skipped_claims)
                conn.commit()
            except PermissionError:
                conn.rollback()
                raise  # not this user's report: record nothing, leak nothing
            except Exception as exc:
                conn.rollback()
                self.record_failure(user_id, report_id, exc)  # separate transaction, after the rollback
                raise
        return {
            "status": status, "sources": len(k.sources), "evidence": len(k.evidence), "claims": len(k.claims),
            "entities": len(k.entities), "skipped_unresolved_evidence": k.skipped_unresolved_evidence,
            "skipped_sources": k.skipped_sources, "skipped_claims": k.skipped_claims,
        }

    def record_failure(self, user_id: str, report_id: str, error: BaseException | str) -> bool:
        """Mark a report's graph ``failed`` (best effort; never raises).

        Runs on its own connection because the failed write was rolled back. The
        graph the rollback preserved (if any) is stale, so it must not read as
        complete. Only the report's owner can have a status recorded.
        """
        message = (f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error))[:_MAX_ERROR]
        try:
            with closing(self.db.get_connection()) as conn:
                owner = conn.execute(
                    "SELECT 1 FROM reports r JOIN research_sessions s ON s.session_id = r.session_id "
                    "WHERE r.report_id = ? AND s.user_id = ?", (report_id, user_id)).fetchone()
                if not owner:
                    return False
                _upsert_status(conn, user_id, report_id, STATUS_FAILED, error=message)
                conn.commit()
            return True
        except Exception:  # the failure handler must never mask the original error
            return False

    def get_status(self, user_id: str, report_id: str) -> Optional[dict[str, Any]]:
        rows = self._rows(
            "SELECT report_id,status,dropped_sources,dropped_evidence,dropped_claims,error,updated_at "
            "FROM knowledge_investigation_status WHERE report_id=? AND user_id=?", (report_id, user_id))
        return rows[0] if rows else None

    def is_complete(self, user_id: str, report_id: str) -> bool:
        st = self.get_status(user_id, report_id)
        return bool(st and st["status"] == STATUS_COMPLETE)

    # ── read (all user-scoped) ───────────────────────────────────────────────
    def _rows(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_claim_trace(self, user_id: str, claim_id: str) -> Optional[dict[str, Any]]:
        """claim → observations, evidence → sources, entities. ``None`` if not this user's."""
        claim = self._rows("SELECT * FROM knowledge_claims WHERE claim_id = ? AND user_id = ?", (claim_id, user_id))
        if not claim:
            return None
        return {
            "claim": claim[0],
            "observations": self._rows(
                "SELECT report_id,status,confidence FROM knowledge_claim_observations WHERE claim_id=? AND user_id=? ORDER BY report_id",
                (claim_id, user_id)),
            "evidence": self._rows(
                "SELECT ce.report_id, ce.stance, ce.strength, e.evidence_id, e.passage_text, "
                "s.source_id, s.url, s.canonical_url, s.domain, s.title "
                "FROM knowledge_claim_evidence ce JOIN knowledge_evidence e ON e.evidence_id=ce.evidence_id "
                "JOIN knowledge_sources s ON s.source_id=e.source_id "
                "WHERE ce.claim_id=? AND ce.user_id=? ORDER BY ce.report_id, e.evidence_id",
                (claim_id, user_id)),
            "entities": self._rows(
                "SELECT DISTINCT en.entity_id, en.name, en.entity_type FROM knowledge_claim_entities ke "
                "JOIN knowledge_entities en ON en.entity_id=ke.entity_id WHERE ke.claim_id=? AND ke.user_id=? ORDER BY en.name",
                (claim_id, user_id)),
        }

    def get_entity_trace(self, user_id: str, entity_id: str) -> Optional[dict[str, Any]]:
        ent = self._rows("SELECT * FROM knowledge_entities WHERE entity_id=? AND user_id=?", (entity_id, user_id))
        if not ent:
            return None
        return {
            "entity": ent[0],
            "claims": self._rows(
                "SELECT DISTINCT c.claim_id, c.claim_text FROM knowledge_claim_entities ke "
                "JOIN knowledge_claims c ON c.claim_id=ke.claim_id WHERE ke.entity_id=? AND ke.user_id=? ORDER BY c.claim_id",
                (entity_id, user_id)),
            "sources": self._rows(
                "SELECT DISTINCT s.source_id, s.url FROM knowledge_evidence_entities ee "
                "JOIN knowledge_evidence e ON e.evidence_id=ee.evidence_id "
                "JOIN knowledge_sources s ON s.source_id=e.source_id WHERE ee.entity_id=? AND ee.user_id=? ORDER BY s.source_id",
                (entity_id, user_id)),
        }

    def list_report_claims(self, user_id: str, report_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT c.claim_id, c.claim_text, o.status, o.confidence FROM knowledge_claim_observations o "
            "JOIN knowledge_claims c ON c.claim_id=o.claim_id WHERE o.report_id=? AND o.user_id=? ORDER BY c.claim_id",
            (report_id, user_id))

    def count(self, user_id: str) -> dict[str, int]:
        out = {}
        for t in ("knowledge_sources", "knowledge_evidence", "knowledge_claims", "knowledge_entities"):
            out[t.removeprefix("knowledge_")] = self._rows(f"SELECT COUNT(*) n FROM {t} WHERE user_id=?", (user_id,))[0]["n"]
        return out

    def integrity_problems(self, user_id: Optional[str] = None) -> list[str]:
        """Operational/self-test check: dangling references and orphans (should be empty).

        With ``user_id`` only that user's rows are examined (used by the
        Knowledge Map summary so one user's report never reflects another's data).
        """
        scope = lambda col: (f" AND {col} = ?", (user_id,)) if user_id else ("", ())  # noqa: E731
        checks = {
            "evidence without source": ("SELECT evidence_id FROM knowledge_evidence WHERE source_id NOT IN (SELECT source_id FROM knowledge_sources)", "user_id"),
            "orphan evidence": ("SELECT evidence_id FROM knowledge_evidence WHERE evidence_id NOT IN (SELECT evidence_id FROM knowledge_claim_evidence)", "user_id"),
            "orphan claim": ("SELECT claim_id FROM knowledge_claims WHERE claim_id NOT IN (SELECT claim_id FROM knowledge_claim_observations)", "user_id"),
            "orphan entity": ("SELECT entity_id FROM knowledge_entities WHERE entity_id NOT IN (SELECT entity_id FROM knowledge_claim_entities UNION SELECT entity_id FROM knowledge_evidence_entities)", "user_id"),
            "orphan source": ("SELECT source_id FROM knowledge_sources WHERE source_id NOT IN (SELECT source_id FROM knowledge_report_sources UNION SELECT source_id FROM knowledge_evidence)", "user_id"),
            "cross-user claim-evidence link": ("SELECT ce.claim_id FROM knowledge_claim_evidence ce JOIN knowledge_claims c ON c.claim_id=ce.claim_id JOIN knowledge_evidence e ON e.evidence_id=ce.evidence_id WHERE (c.user_id<>e.user_id OR c.user_id<>ce.user_id)", "ce.user_id"),
        }
        problems = []
        for name, (sql, col) in checks.items():
            clause, params = scope(col)
            for row in self._rows(sql + clause, params):
                problems.append(f"{name}: {list(row.values())[0]}")
        return problems
