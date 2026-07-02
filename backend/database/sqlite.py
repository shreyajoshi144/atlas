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
            conn.executescript(schema_sql)
            conn.commit()

        logger.info(
            "SQLite database initialized",
            extra={"db_path": self.db_path},
        )