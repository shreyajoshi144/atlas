from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from backend.core.logging import get_logger
from backend.database.sqlite import SQLiteManager
from backend.models.domain_models import (
    AnalyticsEvent,
    RankedSource,
    ResearchReport,
    ResearchSession,
)

logger = get_logger(__name__)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


class TopicRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def get_or_create_topic(self, topic: str, user_id: str) -> str:
        """Topics are scoped per user: two users researching the same topic
        text get separate topic rows, so one user's recent-topics/analytics
        never surface another user's topic. user_id is required — callers
        must derive it from the authenticated user, never from the client."""
        normalized_topic = topic.strip()
        now = utc_now_iso()

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT topic_id FROM topics WHERE topic = ? AND user_id = ?",
                (normalized_topic, user_id),
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE topics SET updated_at = ? WHERE topic_id = ?",
                    (now, row["topic_id"]),
                )
                conn.commit()
                return str(row["topic_id"])

            # uuid-based id (not a hash of the topic text) — two different
            # users picking the same topic text must not collide on
            # topic_id now that topics are no longer globally unique.
            topic_id = f"topic_{uuid.uuid4().hex[:20]}"
            conn.execute(
                """
                INSERT INTO topics (topic_id, user_id, topic, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (topic_id, user_id, normalized_topic, now, now),
            )
            conn.commit()
            return topic_id


class SessionRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def save_session(self, topic_id: str, session: ResearchSession, user_id: str | None = None) -> None:
        metrics = session.metrics
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_sessions (
                    session_id, user_id, topic_id, topic, status, report_id, query_text, error_message,
                    retrieval_latency_ms, scraping_latency_ms, llm_latency_ms, total_execution_ms,
                    prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    user_id,
                    topic_id,
                    session.topic,
                    session.status,
                    session.report_id,
                    session.query_text,
                    session.error_message,
                    metrics.retrieval_latency_ms,
                    metrics.scraping_latency_ms,
                    metrics.llm_latency_ms,
                    metrics.total_execution_ms,
                    metrics.prompt_tokens,
                    metrics.completion_tokens,
                    metrics.total_tokens,
                    metrics.estimated_cost_usd,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            conn.commit()

    def list_sessions(self, limit: int = 20, offset: int = 0, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT session_id, topic_id, topic, status, report_id,
                           total_execution_ms, total_tokens, estimated_cost_usd,
                           created_at, updated_at
                    FROM research_sessions
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT session_id, topic_id, topic, status, report_id,
                           total_execution_ms, total_tokens, estimated_cost_usd,
                           created_at, updated_at
                    FROM research_sessions
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [dict(row) for row in rows]


class ReportRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def save_report(
        self,
        topic_id: str,
        session: ResearchSession,
        report: ResearchReport,
    ) -> None:
        verification = report.verification.model_dump(mode="json") if report.verification else None
        executive_brief = report.executive_brief.model_dump(mode="json") if report.executive_brief else None
        verification_score = (
            report.verification.overall_verification_score if report.verification else None
        )
        verification_verdict = report.verification.verdict if report.verification else None
        now = utc_now_iso()

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO reports (
                    report_id, session_id, topic_id, topic, summary, report_markdown,
                    key_statistics_json, swot_json, verification_score, verification_verdict,
                    verification_json, executive_brief_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    session.session_id,
                    topic_id,
                    report.topic,
                    report.summary,
                    report.report_markdown,
                    json.dumps(report.key_statistics, ensure_ascii=False),
                    json.dumps(report.swot, ensure_ascii=False),
                    verification_score,
                    verification_verdict,
                    json.dumps(verification, ensure_ascii=False) if verification else None,
                    json.dumps(executive_brief, ensure_ascii=False) if executive_brief else None,
                    now,
                    now,
                ),
            )
            conn.commit()

    def list_reports(
        self,
        limit: int = 20,
        offset: int = 0,
        topic: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            base = """
                SELECT r.report_id, r.session_id, r.topic_id, r.topic, r.summary,
                       r.verification_score, r.verification_verdict, r.created_at, r.updated_at
                FROM reports r
                JOIN research_sessions s ON s.session_id = r.session_id
                WHERE s.user_id = ?
            """
            params: list[Any] = [user_id]
            if topic:
                base += " AND r.topic LIKE ?"
                params.append(f"%{topic}%")
            base += " ORDER BY r.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(base, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def count_reports(self, topic: str | None = None, user_id: str | None = None) -> int:
        with self.db.get_connection() as conn:
            base = """
                SELECT COUNT(*)
                FROM reports r
                JOIN research_sessions s ON s.session_id = r.session_id
                WHERE s.user_id = ?
            """
            params: list[Any] = [user_id]
            if topic:
                base += " AND r.topic LIKE ?"
                params.append(f"%{topic}%")
            row = conn.execute(base, tuple(params)).fetchone()
        return row[0] if row else 0

    def get_report(self, report_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
            if user_id:
                row = conn.execute(
                    """
                    SELECT r.report_id, r.session_id, r.topic_id, r.topic, r.summary, r.report_markdown,
                           r.key_statistics_json, r.swot_json, r.verification_score, r.verification_verdict,
                           r.verification_json, r.executive_brief_json, r.created_at, r.updated_at
                    FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE r.report_id = ? AND s.user_id = ?
                    """,
                    (report_id, user_id),
                ).fetchone()
            else:
                # Internal/unscoped lookup only — e.g. search.py enriching its own
                # already-user-filtered vector results. Never expose this path
                # directly to a request without a user_id.
                row = conn.execute(
                    """
                    SELECT report_id, session_id, topic_id, topic, summary, report_markdown,
                           key_statistics_json, swot_json, verification_score, verification_verdict,
                           verification_json, executive_brief_json, created_at, updated_at
                    FROM reports
                    WHERE report_id = ?
                    """,
                    (report_id,),
                ).fetchone()
        return dict(row) if row else None

    def is_owned_by(self, report_id: str, user_id: str) -> bool:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM reports r
                JOIN research_sessions s ON s.session_id = r.session_id
                WHERE r.report_id = ? AND s.user_id = ?
                """,
                (report_id, user_id),
            ).fetchone()
        return row is not None

    def delete_report(self, report_id: str) -> bool:
        from backend.knowledge.repository import purge_report_knowledge  # lazy: avoids an import cycle

        with self.db.get_connection() as conn:
            # M5: knowledge links + orphan GC, and evaluations (FK to reports —
            # without this, deleting any evaluated report raised IntegrityError).
            purge_report_knowledge(conn, report_id)
            conn.execute("DELETE FROM evaluations WHERE report_id = ?", (report_id,))
            conn.execute("DELETE FROM notes WHERE report_id = ?", (report_id,))
            conn.execute("DELETE FROM report_chunks WHERE report_id = ?", (report_id,))
            conn.execute("DELETE FROM sources WHERE report_id = ?", (report_id,))
            cursor = conn.execute("DELETE FROM reports WHERE report_id = ?", (report_id,))
            conn.commit()
        return cursor.rowcount > 0

    def get_all_report_ids(self, user_id: str | None = None) -> list[str]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT r.report_id FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    ORDER BY r.created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT report_id FROM reports ORDER BY created_at DESC"
                ).fetchall()
        return [row[0] for row in rows]

    def get_recent_topics(self, limit: int = 10, user_id: str | None = None) -> list[str]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT DISTINCT r.topic FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    ORDER BY r.created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT topic FROM reports ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [row[0] for row in rows]


class SourceRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def replace_sources(
        self,
        report_id: str,
        session_id: str,
        sources: list[RankedSource],
    ) -> None:
        now = utc_now_iso()

        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sources WHERE report_id = ?", (report_id,))
            for source in sources:
                conn.execute(
                    """
                    INSERT INTO sources (
                        source_id, report_id, session_id, title, url, domain, snippet,
                        publish_date, bm25_score, tfidf_score, cosine_score, fuzz_score,
                        relevance_score, credibility_score, rank_position, source_type, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.source_id,
                        report_id,
                        session_id,
                        source.title,
                        str(source.url),
                        source.domain,
                        source.snippet,
                        source.publish_date.isoformat() if source.publish_date else None,
                        source.bm25_score,
                        source.tfidf_score,
                        source.cosine_score,
                        source.fuzz_score,
                        source.relevance_score,
                        source.credibility_score,
                        source.rank_position,
                        str(source.source_type),
                        now,
                    ),
                )
            conn.commit()

    # Alias kept for backward compat — prefer replace_sources() for bulk saves
    def save_source(self, report_id: str, source: RankedSource, session_id: str = "") -> None:
        now = utc_now_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sources (
                    source_id, report_id, session_id, title, url, domain, snippet,
                    publish_date, bm25_score, tfidf_score, cosine_score, fuzz_score,
                    relevance_score, credibility_score, rank_position, source_type, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.source_id,
                    report_id,
                    session_id,
                    source.title,
                    str(source.url),
                    source.domain,
                    source.snippet,
                    source.publish_date.isoformat() if source.publish_date else None,
                    source.bm25_score,
                    source.tfidf_score,
                    source.cosine_score,
                    source.fuzz_score,
                    source.relevance_score,
                    source.credibility_score,
                    source.rank_position,
                    str(source.source_type),
                    now,
                ),
            )
            conn.commit()

    def get_sources_by_report_id(self, report_id: str) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT source_id, report_id, session_id, title, url, domain, snippet,
                       publish_date, relevance_score, credibility_score, rank_position
                FROM sources
                WHERE report_id = ?
                ORDER BY rank_position ASC
                """,
                (report_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_all_sources(self, user_id: str | None = None) -> int:
        with self.db.get_connection() as conn:
            if user_id:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM sources src
                    JOIN research_sessions s ON s.session_id = src.session_id
                    WHERE s.user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM sources").fetchone()
        return row[0] if row else 0

    def get_domain_distribution(self, limit: int = 10, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT src.domain, COUNT(*) as count
                    FROM sources src
                    JOIN research_sessions s ON s.session_id = src.session_id
                    WHERE s.user_id = ?
                    GROUP BY src.domain
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT domain, COUNT(*) as count
                    FROM sources
                    GROUP BY domain
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [{"domain": row[0], "count": row[1]} for row in rows]


class ReportChunkRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def replace_chunks(
        self,
        report_id: str,
        session_id: str,
        topic_id: str,
        topic: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        now = utc_now_iso()

        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM report_chunks WHERE report_id = ?", (report_id,))
            for chunk in chunks:
                conn.execute(
                    """
                    INSERT INTO report_chunks (
                        chunk_id, report_id, session_id, topic_id, topic,
                        chunk_index, content, char_count, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        report_id,
                        session_id,
                        topic_id,
                        topic,
                        chunk["chunk_index"],
                        chunk["content"],
                        len(chunk["content"]),
                        now,
                    ),
                )
            conn.commit()

    def get_chunks_by_report_id(self, report_id: str) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, report_id, session_id, topic_id, topic,
                       chunk_index, content, char_count, created_at
                FROM report_chunks
                WHERE report_id = ?
                ORDER BY chunk_index ASC
                """,
                (report_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_chunks_for_report(self, report_id: str, user_id: str) -> int:
        """Delete a report's chunk rows, only if the report's session belongs to user_id."""
        if not user_id:
            raise ValueError("user_id is required")
        with self.db.get_connection() as conn:
            cur = conn.execute(
                """
                DELETE FROM report_chunks
                WHERE report_id = ?
                  AND session_id IN (SELECT session_id FROM research_sessions WHERE user_id = ?)
                """,
                (report_id, user_id),
            )
            conn.commit()
            return cur.rowcount

    def get_user_chunks(
        self,
        user_id: str,
        topic: str | None = None,
        report_ids: list[str] | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """All of ONE user's chunks (ownership enforced by joining on the
        session's user_id), newest first, as hybrid-retrieval candidates."""
        if not user_id:
            raise ValueError("user_id is required")
        clauses = ["s.user_id = ?"]
        params: list[Any] = [user_id]
        if topic:
            clauses.append("c.topic = ?")
            params.append(topic)
        if report_ids:
            clauses.append("c.report_id IN (%s)" % ",".join("?" * len(report_ids)))
            params.extend(report_ids)
        params.append(int(limit))
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.report_id, c.session_id, c.topic, c.chunk_index,
                       c.content, c.created_at, r.verification_score
                FROM report_chunks c
                JOIN research_sessions s ON s.session_id = c.session_id
                JOIN reports r ON r.report_id = c.report_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.created_at DESC, c.report_id, c.chunk_index
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_all_chunks(self) -> int:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM report_chunks").fetchone()
        return row[0] if row else 0


class AnalyticsRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def save_event(self, event: AnalyticsEvent) -> None:
        now = utc_now_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO analytics_events (
                    event_id, session_id, event_type, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.session_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_events_for_session(self, session_id: str, user_id: str) -> list[dict[str, Any]]:
        """Return analytics events only for a session owned by user_id."""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.event_id, e.session_id, e.event_type, e.payload_json, e.created_at
                FROM analytics_events e
                JOIN research_sessions s ON s.session_id = e.session_id
                WHERE e.session_id = ? AND s.user_id = ?
                ORDER BY e.created_at ASC
                """,
                (session_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reports_over_time(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT DATE(r.created_at) as date, COUNT(*) as count
                    FROM reports r JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    GROUP BY DATE(r.created_at)
                    ORDER BY date ASC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT DATE(created_at) as date, COUNT(*) as count
                    FROM reports
                    GROUP BY DATE(created_at)
                    ORDER BY date ASC
                    """,
                ).fetchall()
        return [{"date": row[0], "count": row[1]} for row in rows]

    def get_verification_trend(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT DATE(r.created_at) as date,
                           ROUND(AVG(r.verification_score), 3) as avg_score
                    FROM reports r JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE r.verification_score IS NOT NULL AND s.user_id = ?
                    GROUP BY DATE(r.created_at)
                    ORDER BY date ASC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT DATE(created_at) as date,
                           ROUND(AVG(verification_score), 3) as avg_score
                    FROM reports
                    WHERE verification_score IS NOT NULL
                    GROUP BY DATE(created_at)
                    ORDER BY date ASC
                    """,
                ).fetchall()
        return [{"date": row[0], "avg_score": row[1]} for row in rows]

    def get_top_topics(self, limit: int = 10, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT r.topic, COUNT(*) as count
                    FROM reports r JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    GROUP BY r.topic
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT topic, COUNT(*) as count
                    FROM reports
                    GROUP BY topic
                    ORDER BY count DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [{"topic": row[0], "count": row[1]} for row in rows]

    def get_totals(self, user_id: str | None = None) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            if user_id:
                row = conn.execute(
                    """
                    SELECT COUNT(*) as total_reports,
                           ROUND(AVG(r.verification_score), 3) as avg_verification_score
                    FROM reports r JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                session_row = conn.execute(
                    """
                    SELECT SUM(total_execution_ms) as total_exec_ms,
                           AVG(total_execution_ms) as avg_exec_ms,
                           SUM(total_tokens) as total_tokens,
                           SUM(estimated_cost_usd) as total_cost,
                           AVG(retrieval_latency_ms) as avg_retrieval_ms
                    FROM research_sessions
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) as total_reports,
                        ROUND(AVG(verification_score), 3) as avg_verification_score
                    FROM reports
                    """,
                ).fetchone()
                session_row = conn.execute(
                    """
                    SELECT
                        SUM(total_execution_ms) as total_exec_ms,
                        AVG(total_execution_ms) as avg_exec_ms,
                        SUM(total_tokens) as total_tokens,
                        SUM(estimated_cost_usd) as total_cost,
                        AVG(retrieval_latency_ms) as avg_retrieval_ms
                    FROM research_sessions
                    """,
                ).fetchone()
        return {
            "total_reports": row[0] if row else 0,
            "avg_verification_score": row[1] if row and row[1] else 0.0,
            "total_exec_ms": session_row[0] if session_row and session_row[0] else 0,
            "avg_exec_ms": session_row[1] if session_row and session_row[1] else 0.0,
            "total_tokens": session_row[2] if session_row and session_row[2] else 0,
            "total_cost": session_row[3] if session_row and session_row[3] else 0.0,
            "avg_retrieval_ms": session_row[4] if session_row and session_row[4] else 0.0,
        }


class KnowledgeBaseRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def get_overview(self, user_id: str | None = None) -> dict[str, Any]:
        with self.db.get_connection() as conn:
            if user_id:
                total_reports = conn.execute(
                    """
                    SELECT COUNT(*) FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    """,
                    (user_id,),
                ).fetchone()[0]
                total_chunks = conn.execute(
                    """
                    SELECT COUNT(*) FROM report_chunks c
                    JOIN research_sessions s ON s.session_id = c.session_id
                    WHERE s.user_id = ?
                    """,
                    (user_id,),
                ).fetchone()[0]
                recent_topic_rows = conn.execute(
                    """
                    SELECT DISTINCT r.topic FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    ORDER BY r.created_at DESC LIMIT 10
                    """,
                    (user_id,),
                ).fetchall()
                report_id_rows = conn.execute(
                    """
                    SELECT r.report_id FROM reports r
                    JOIN research_sessions s ON s.session_id = r.session_id
                    WHERE s.user_id = ?
                    ORDER BY r.created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                total_reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
                total_chunks = conn.execute("SELECT COUNT(*) FROM report_chunks").fetchone()[0]
                recent_topic_rows = conn.execute(
                    "SELECT DISTINCT topic FROM reports ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
                report_id_rows = conn.execute(
                    "SELECT report_id FROM reports ORDER BY created_at DESC"
                ).fetchall()
        return {
            "total_reports": total_reports,
            "total_chunks": total_chunks,
            "recent_topics": [r[0] for r in recent_topic_rows],
            "report_ids": [r[0] for r in report_id_rows],
        }


class UserRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def create(self, email: str, name: str, password_hash: str, password_salt: str) -> dict[str, Any]:
        user_id = f"user_{uuid.uuid4().hex[:20]}"
        now = utc_now_iso()
        normalized_email = email.strip().lower()

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, email, name, password_hash, password_salt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, normalized_email, name.strip(), password_hash, password_salt, now, now),
            )
            conn.commit()

        return {
            "user_id": user_id,
            "email": normalized_email,
            "name": name.strip(),
            "created_at": now,
            "updated_at": now,
        }

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        normalized_email = email.strip().lower()
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?",
                (normalized_email,),
            ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_password_hash(self, user_id: str, password_hash: str, password_salt: str) -> None:
        """Used both by password reset and by the login-time Argon2id
        upgrade path for accounts still on a legacy PBKDF2 hash."""
        now = utc_now_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, updated_at = ? WHERE user_id = ?",
                (password_hash, password_salt, now, user_id),
            )
            conn.commit()


class PasswordResetRepository:
    """Reset tokens are stored only as a SHA-256 hash (token_hash) — the
    raw token is never persisted anywhere, only handed to the user once via
    the reset link. Single-use (used_at) and time-limited (expires_at)."""

    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def create(self, user_id: str, token_hash: str, expires_minutes: int) -> str:
        # Naive UTC throughout, matching utc_now_iso() elsewhere in this
        # module — mixing naive and timezone-aware ISO strings would break
        # the plain lexicographic comparison used to check expiry below.
        token_id = f"reset_{uuid.uuid4().hex[:20]}"
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=expires_minutes)).isoformat()

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO password_reset_tokens (token_id, user_id, token_hash, expires_at, used_at, created_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (token_id, user_id, token_hash, expires_at, now.isoformat()),
            )
            conn.commit()
        return token_id

    def get_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM password_reset_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return dict(row) if row else None

    def mark_used(self, token_id: str) -> None:
        now = utc_now_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE token_id = ?",
                (now, token_id),
            )
            conn.commit()


class NoteRepository:
    """One note per (user, report) — plain student notes, kept deliberately
    simple and fully separate from Atlas-generated report content."""

    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def get_note(self, user_id: str, report_id: str) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT note_id, user_id, report_id, content, created_at, updated_at "
                "FROM notes WHERE user_id = ? AND report_id = ?",
                (user_id, report_id),
            ).fetchone()
        return dict(row) if row else None

    def upsert_note(self, user_id: str, report_id: str, content: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self.db.get_connection() as conn:
            existing = conn.execute(
                "SELECT note_id, created_at FROM notes WHERE user_id = ? AND report_id = ?",
                (user_id, report_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE notes SET content = ?, updated_at = ? WHERE note_id = ?",
                    (content, now, existing["note_id"]),
                )
                conn.commit()
                return {
                    "note_id": existing["note_id"], "user_id": user_id, "report_id": report_id,
                    "content": content, "created_at": existing["created_at"], "updated_at": now,
                }
            note_id = f"note_{uuid.uuid4().hex[:20]}"
            conn.execute(
                """
                INSERT INTO notes (note_id, user_id, report_id, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (note_id, user_id, report_id, content, now, now),
            )
            conn.commit()
            return {
                "note_id": note_id, "user_id": user_id, "report_id": report_id,
                "content": content, "created_at": now, "updated_at": now,
            }

    def delete_note(self, user_id: str, report_id: str) -> bool:
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM notes WHERE user_id = ? AND report_id = ?",
                (user_id, report_id),
            )
            conn.commit()
        return cursor.rowcount > 0


class EvaluationRepository:
    """Persists backend.evaluation.models.EvaluationSummary results.

    Deliberately independent of ReportRepository/SessionRepository: a
    research run's report/sources/session are always saved first (see
    api/routes/research.py), and this table is written afterwards, on a
    best-effort basis. A missing evaluation row is never treated as an
    error by any caller.
    """

    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def save_evaluation(
        self,
        report_id: str,
        session_id: str,
        topic_id: str | None,
        summary: Any,
    ) -> str:
        evaluation_id = f"eval_{uuid.uuid4().hex[:20]}"
        now = utc_now_iso()
        details = summary.model_dump(mode="json")

        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO evaluations (
                    evaluation_id, report_id, session_id, topic_id, overall_score,
                    retrieval_score, grounding_score, verification_quality_score,
                    answer_groundedness_score, research_quality_score, agent_quality_score,
                    total_latency_ms, tool_call_count, replan_count, total_tokens, estimated_cost_usd,
                    flags_json, details_json, errors_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    report_id,
                    session_id,
                    topic_id,
                    summary.overall_score,
                    summary.retrieval.score if summary.retrieval else None,
                    summary.grounding.score if summary.grounding else None,
                    summary.verification_quality.score if summary.verification_quality else None,
                    summary.answer_groundedness.score if summary.answer_groundedness else None,
                    summary.research_quality.score if summary.research_quality else None,
                    summary.agent_quality.score if summary.agent_quality else None,
                    summary.performance.total_latency_ms if summary.performance else None,
                    summary.performance.tool_call_count if summary.performance else None,
                    summary.performance.replan_count if summary.performance else None,
                    summary.performance.total_tokens if summary.performance else None,
                    summary.performance.estimated_cost_usd if summary.performance else None,
                    json.dumps([f.model_dump() for f in summary.flags], ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    json.dumps(summary.errors, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        return evaluation_id

    def get_latest_by_report_id(self, report_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT e.evaluation_id, e.report_id, e.session_id, e.topic_id, e.overall_score,
                       e.retrieval_score, e.grounding_score, e.verification_quality_score,
                       e.answer_groundedness_score, e.research_quality_score, e.agent_quality_score,
                       e.total_latency_ms, e.tool_call_count, e.replan_count, e.total_tokens, e.estimated_cost_usd,
                       e.flags_json, e.details_json, e.errors_json, e.created_at
                FROM evaluations e
                JOIN research_sessions rs ON rs.session_id = e.session_id
                WHERE e.report_id = ? AND (? IS NULL OR rs.user_id = ?)
                ORDER BY e.created_at DESC
                LIMIT 1
                """,
                (report_id, user_id, user_id),
            ).fetchone()
        return dict(row) if row else None


class ResearchRepository:
    def __init__(self, db: SQLiteManager | None = None) -> None:
        self.db = db or SQLiteManager()
        self.topics = TopicRepository(self.db)
        self.sessions = SessionRepository(self.db)
        self.reports = ReportRepository(self.db)
        self.sources = SourceRepository(self.db)
        self.chunks = ReportChunkRepository(self.db)
        self.analytics = AnalyticsRepository(self.db)
        self.knowledge_base = KnowledgeBaseRepository(self.db)
        self.users = UserRepository(self.db)
        self.notes = NoteRepository(self.db)
        self.password_resets = PasswordResetRepository(self.db)
        self.evaluations = EvaluationRepository(self.db)

    def initialize(self) -> None:
        self.db.initialize_database()
