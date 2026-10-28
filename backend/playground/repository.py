from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from backend.database.sqlite import SQLiteManager


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlaygroundRepository:
    """Small per-user conversation store; every lookup is owner-scoped."""

    MAX_MESSAGES = 200

    def __init__(self, db: SQLiteManager) -> None:
        self.db = db

    def list(self, user_id: str) -> list[dict[str, Any]]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                "SELECT conversation_id,title,created_at,updated_at,messages_json FROM playground_conversations "
                "WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
            ).fetchall()
        return [{"conversation_id": row["conversation_id"], "title": row["title"],
                 "created_at": row["created_at"], "updated_at": row["updated_at"],
                 "message_count": len(json.loads(row["messages_json"] or "[]"))} for row in rows]

    def get(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                "SELECT conversation_id,title,created_at,updated_at,messages_json FROM playground_conversations "
                "WHERE conversation_id=? AND user_id=?", (conversation_id, user_id)
            ).fetchone()
        if row is None:
            return None
        return {"conversation_id": row["conversation_id"], "title": row["title"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "messages": json.loads(row["messages_json"] or "[]")}

    def save_turn(self, user_id: str, conversation_id: str | None, title: str,
                  user_message: dict[str, Any], assistant_message: dict[str, Any]) -> dict[str, Any]:
        now = _iso()
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conversation_id is None:
                conversation_id = f"conv_{uuid.uuid4().hex}"
                messages: list[dict[str, Any]] = []
                conn.execute("INSERT INTO playground_conversations(conversation_id,user_id,title,messages_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                             (conversation_id, user_id, title[:120], "[]", now, now))
            else:
                row = conn.execute("SELECT messages_json FROM playground_conversations WHERE conversation_id=? AND user_id=?",
                                   (conversation_id, user_id)).fetchone()
                if row is None:
                    conn.rollback()
                    raise LookupError("Conversation not found")
                messages = json.loads(row["messages_json"] or "[]")
            messages.extend((user_message, assistant_message))
            messages = messages[-self.MAX_MESSAGES:]
            conn.execute("UPDATE playground_conversations SET messages_json=?,updated_at=? WHERE conversation_id=? AND user_id=?",
                         (json.dumps(messages, ensure_ascii=False), now, conversation_id, user_id))
            conn.commit()
        return self.get(user_id, conversation_id)  # type: ignore[return-value]

    def delete(self, user_id: str, conversation_id: str) -> bool:
        with closing(self.db.get_connection()) as conn:
            cur = conn.execute("DELETE FROM playground_conversations WHERE conversation_id=? AND user_id=?",
                               (conversation_id, user_id))
            conn.commit()
            return cur.rowcount == 1
