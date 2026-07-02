from __future__ import annotations

import json
from datetime import datetime
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

    def get_or_create_topic(self, topic: str) -> str:
        normalized_topic = topic.strip()
        now = utc_now_iso()

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT topic_id FROM topics WHERE topic = ?",
                (normalized_topic,),
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE topics SET updated_at = ? WHERE topic_id = ?",
                    (now, row["topic_id"]),
                )
                conn.commit()
                return str(row["topic_id"])

            topic_id = f"topic_{abs(hash(normalized_topic))}"
            conn.execute(
                """
                INSERT INTO topics (topic_id, topic, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (topic_id, normalized_topic, now, now),
            )
            conn.commit()
            return topic_id


class SessionRepository:
    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def save_session(self, topic_id: str, session: ResearchSession) -> None:
        metrics = session.metrics
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_sessions (
                    session_id, topic_id, topic, status, report_id, query_text, error_message,
                    retrieval_latency_ms, scraping_latency_ms, llm_latency_ms, total_execution_ms,
                    prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
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

    def list_sessions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
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
    ) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            if topic:
                rows = conn.execute(
                    """
                    SELECT report_id, session_id, topic_id, topic, summary,
                           verification_score, verification_verdict, created_at, updated_at
                    FROM reports
                    WHERE topic LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (f"%{topic}%", limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT report_id, session_id, topic_id, topic, summary,
                           verification_score, verification_verdict, created_at, updated_at
                    FROM reports
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [dict(row) for row in rows]

    def count_reports(self, topic: str | None = None) -> int:
        with self.db.get_connection() as conn:
            if topic:
                row = conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE topic LIKE ?",
                    (f"%{topic}%",),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM reports").fetchone()
        return row[0] if row else 0

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self.db.get_connection() as conn:
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

    def delete_report(self, report_id: str) -> bool:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM report_chunks WHERE report_id = ?", (report_id,))
            conn.execute("DELETE FROM sources WHERE report_id = ?", (report_id,))
            cursor = conn.execute("DELETE FROM reports WHERE report_id = ?", (report_id,))
            conn.commit()
        return cursor.rowcount > 0

    def get_all_report_ids(self) -> list[str]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT report_id FROM reports ORDER BY created_at DESC"
            ).fetchall()
        return [row[0] for row in rows]

    def get_recent_topics(self, limit: int = 10) -> list[str]:
        with self.db.get_connection() as conn:
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

    # Alias kept for backward compat
    def save_source(self, report_id: str, source: RankedSource) -> None:
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
                    "",
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

    def count_all_sources(self) -> int:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM sources").fetchone()
        return row[0] if row else 0

    def get_domain_distribution(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
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

    def get_reports_over_time(self) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DATE(created_at) as date, COUNT(*) as count
                FROM reports
                GROUP BY DATE(created_at)
                ORDER BY date ASC
                """,
            ).fetchall()
        return [{"date": row[0], "count": row[1]} for row in rows]

    def get_verification_trend(self) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
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

    def get_top_topics(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.get_connection() as conn:
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

    def get_totals(self) -> dict[str, Any]:
        with self.db.get_connection() as conn:
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

    def get_overview(self) -> dict[str, Any]:
        with self.db.get_connection() as conn:
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

    def initialize(self) -> None:
        self.db.initialize_database()
