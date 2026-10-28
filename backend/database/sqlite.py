from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

class SQLiteManager:
    def __init__(self, db_path: str | None = None) -> None:
        settings = get_settings()
        self.db_path = db_path or settings.sqlite_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        return connection

    def initialize_database(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        schema_sql = schema_path.read_text(encoding="utf-8")

        with self.get_connection() as conn:
            # Column/shape migrations must run BEFORE the schema script:
            # schema.sql's CREATE INDEX statements reference columns
            # (research_sessions.user_id, topics.user_id) that a database
            # created before this change won't have yet. Running migrations
            # first means those columns already exist by the time the
            # (idempotent) CREATE TABLE/INDEX IF NOT EXISTS statements run.
            self._migrate_add_session_user_id(conn)
            self._migrate_topics_user_scope(conn)
            self._migrate_evaluation_columns(conn)
            self._migrate_user_roles(conn)
            self._migrate_budget_accounting(conn)
            conn.executescript(schema_sql)
            conn.commit()

        logger.info(
            "SQLite database initialized",
            extra={"db_path": self.db_path},
        )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _migrate_add_session_user_id(self, conn: sqlite3.Connection) -> None:
        """CREATE TABLE IF NOT EXISTS never adds columns to a table that
        already exists, so a database created before user-scoped research
        sessions needs this column added by hand, once. Safe to run on
        every startup — it's a no-op once the column is present, and a
        no-op on a brand-new database (the table doesn't exist yet, so
        schema.sql's own CREATE TABLE below will create it correctly)."""
        if not self._table_exists(conn, "research_sessions"):
            return
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(research_sessions)").fetchall()}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE research_sessions ADD COLUMN user_id TEXT REFERENCES users (user_id)")
            conn.commit()
            logger.info("Migrated research_sessions: added user_id column")


    def _migrate_evaluation_columns(self, conn: sqlite3.Connection) -> None:
        """Add Milestone 2 summary columns to pre-existing evaluation rows.
        The complete structured evaluation remains in details_json; these
        columns make common metrics queryable without changing run semantics.
        """
        if not self._table_exists(conn, "evaluations"):
            return
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        additions = {
            "research_quality_score": "REAL",
            "agent_quality_score": "REAL",
            "total_latency_ms": "INTEGER",
            "tool_call_count": "INTEGER",
            "replan_count": "INTEGER",
            "total_tokens": "INTEGER",
            "estimated_cost_usd": "REAL",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE evaluations ADD COLUMN {name} {sql_type}")
        conn.commit()

    def _migrate_user_roles(self, conn: sqlite3.Connection) -> None:
        """Add an explicit, least-privilege role to existing accounts."""
        if not self._table_exists(conn, "users"):
            return
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin'))")
            conn.commit()
            logger.info("Migrated users: added role column with user default")

    def _migrate_budget_accounting(self, conn: sqlite3.Connection) -> None:
        """Retain original reservations and distinguish unknown cost from zero cost."""
        if not self._table_exists(conn, "llm_budget_events"):
            return
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(llm_budget_events)").fetchall()}
        if "reserved_cost_usd" not in existing:
            conn.execute("ALTER TABLE llm_budget_events ADD COLUMN reserved_cost_usd REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE llm_budget_events SET reserved_cost_usd=estimated_cost_usd")
            logger.info("Migrated llm_budget_events: preserved original reservation amounts")
        if "cost_known" not in existing:
            conn.execute("ALTER TABLE llm_budget_events ADD COLUMN cost_known INTEGER NOT NULL DEFAULT 1 CHECK (cost_known IN (0,1))")
            # Older versions stored unknown estimates as zero. Since those
            # rows cannot be distinguished from an actual zero charge, treat
            # them as unknown rather than silently freeing budget capacity.
            conn.execute("UPDATE llm_budget_events SET cost_known=0 WHERE estimated_cost_usd=0 AND actual_cost_usd IS NULL")
            logger.info("Migrated llm_budget_events: marked ambiguous zero-cost rows unknown")
        conn.commit()

    def _migrate_topics_user_scope(self, conn: sqlite3.Connection) -> None:
        """Topics predate per-user scoping and were globally UNIQUE by topic
        text alone. SQLite can't ALTER a UNIQUE constraint in place, so a
        database created before this change needs the table rebuilt once:
        add user_id and switch the uniqueness to UNIQUE(user_id, topic).
        topic_id values are preserved so existing FKs (research_sessions,
        reports, report_chunks) keep resolving. Existing rows are left with
        user_id = NULL rather than guessing an owner — SQLite treats NULL as
        distinct for UNIQUE purposes, so this never blocks a new per-user
        topic from being created, and every session/report row under these
        topics is still correctly scoped via its own user_id column,
        independent of this table. Safe to run on every startup — it's a
        no-op once the column is present, and a no-op on a brand-new
        database (schema.sql's own CREATE TABLE below creates it correctly)."""
        if not self._table_exists(conn, "topics"):
            return
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
        if "user_id" in cols:
            return

        logger.info("Migrating topics table to per-user scoping")
        conn.execute("PRAGMA foreign_keys = OFF")
        # legacy_alter_table=ON stops SQLite from "helpfully" rewriting other
        # tables' REFERENCES clauses (research_sessions, reports,
        # report_chunks all reference topics(topic_id)) to point at
        # "topics_old" when it gets renamed below — without this, those
        # tables silently end up referencing a table we're about to drop,
        # which fails PRAGMA foreign_key_check even though every topic_id
        # value itself is still valid and unchanged.
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            conn.execute("ALTER TABLE topics RENAME TO topics_old")
            conn.execute(
                """
                CREATE TABLE topics (
                    topic_id TEXT PRIMARY KEY,
                    user_id TEXT REFERENCES users (user_id),
                    topic TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, topic)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO topics (topic_id, user_id, topic, created_at, updated_at)
                SELECT topic_id, NULL, topic, created_at, updated_at FROM topics_old
                """
            )
            conn.execute("DROP TABLE topics_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topics_topic ON topics (topic)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_topics_user_id ON topics (user_id)")
            conn.commit()
            logger.info("Migrated topics: added user_id column and per-user UNIQUE constraint")
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")
