"""App-wide monthly spend ceiling: bounds total spend regardless of how many accounts exist."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.database.sqlite import SQLiteManager
from backend.ops.repository import OperationalRepository, UsageLimitExceeded


def _db_with_users(path, *user_ids):
    db = SQLiteManager(str(path))
    db.initialize_database()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        for uid in user_ids:
            conn.execute(
                "INSERT INTO users(user_id,email,name,password_hash,password_salt,role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@example.test", uid, "hash", "salt", "user", now, now),
            )
        conn.commit()
    return db


def _reserve(repo, user, estimate, *, global_spend):
    return repo.reserve_call(user, "test", "model-a", estimate, default_calls=1000, default_spend=None,
                             global_spend=global_spend)


def test_global_cap_is_shared_across_accounts(tmp_path):
    repo = OperationalRepository(_db_with_users(tmp_path / "g.db", "alice", "bob"))
    _reserve(repo, "alice", 0.6, global_spend=1.0)  # no per-user cap, so only the global one applies
    with pytest.raises(UsageLimitExceeded, match="service has reached its monthly usage limit"):
        _reserve(repo, "bob", 0.6, global_spend=1.0)  # a different account is still blocked
    _reserve(repo, "bob", 0.3, global_spend=1.0)      # a call that fits is still allowed


def test_global_cap_blocks_unpriced_calls(tmp_path):
    repo = OperationalRepository(_db_with_users(tmp_path / "g2.db", "alice"))
    with pytest.raises(UsageLimitExceeded, match="cannot be safely estimated"):
        _reserve(repo, "alice", None, global_spend=5.0)


def test_no_global_cap_means_no_ceiling(tmp_path):
    repo = OperationalRepository(_db_with_users(tmp_path / "g3.db", "alice"))
    for _ in range(3):
        _reserve(repo, "alice", 100.0, global_spend=None)
