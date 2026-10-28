"""Isolated persistence tests for terminalizing exhausted stale AtlasBench leases."""
from datetime import datetime, timedelta, timezone

from backend.bench.models import MAX_RUN_ATTEMPTS, RUN_LEASE_SECONDS
from backend.bench.repository import BenchRepository
from backend.database.sqlite import SQLiteManager


def test_exhausted_stale_run_is_failed_but_retryable_stale_run_is_not(tmp_path):
    db = SQLiteManager(str(tmp_path / "bench-recovery.sqlite"))
    now = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
    stale = (now - timedelta(seconds=RUN_LEASE_SECONDS + 1)).isoformat()
    with db.get_connection() as conn:
        conn.execute("""CREATE TABLE bench_runs (
            run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL, updated_at TEXT NOT NULL,
            completed_at TEXT, error_type TEXT
        )""")
        conn.executemany(
            "INSERT INTO bench_runs(run_id,user_id,status,attempt_count,updated_at) VALUES(?,?,?,?,?)",
            [("exhausted", "alice", "running", MAX_RUN_ATTEMPTS, stale),
             ("retryable", "alice", "running", MAX_RUN_ATTEMPTS - 1, stale),
             ("other-user", "bob", "running", MAX_RUN_ATTEMPTS, stale)],
        )
        conn.commit()

    repo = BenchRepository(db)
    repo._terminalize_exhausted_stale_runs("alice", now)
    with db.get_connection() as conn:
        rows = {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM bench_runs")}
    assert rows["exhausted"]["status"] == "failed"
    assert rows["exhausted"]["error_type"] == "interrupted_max_attempts_exhausted"
    assert rows["exhausted"]["completed_at"] == now.isoformat()
    assert rows["retryable"]["status"] == "running"
    assert rows["other-user"]["status"] == "running"  # reconciliation remains user-scoped
