"""Lets tests use the REAL SQLite manager, schema.sql and budget repository without pydantic-settings/FastAPI.

If the real app dependencies are installed (CI / a full install) nothing is stubbed.
"""
from __future__ import annotations

import sys
import tempfile
import types
import uuid
from pathlib import Path
from types import SimpleNamespace


def install() -> None:
    try:
        import pydantic_settings  # noqa: F401
        import backend.core.config  # noqa: F401
        return
    except Exception:
        pass
    tmp = Path(tempfile.gettempdir()) / "atlas-provider-tests"
    tmp.mkdir(exist_ok=True)
    mod = types.ModuleType("backend.core.config")
    mod.LOG_DIR = tmp
    mod.get_settings = lambda: SimpleNamespace(sqlite_path=str(tmp / "default.db"), app_env="development",
                                              log_level="INFO", default_daily_llm_call_limit=1000,
                                              default_monthly_llm_spend_cap_usd=None)
    sys.modules["backend.core.config"] = mod


def make_db(user_ids=("u1", "u2")):
    install()
    from backend.database.sqlite import SQLiteManager
    path = Path(tempfile.mkdtemp(prefix="atlas-cmp-")) / f"{uuid.uuid4().hex}.db"
    db = SQLiteManager(str(path))
    db.initialize_database()
    from contextlib import closing
    with closing(db.get_connection()) as conn:
        for u in user_ids:
            conn.execute("INSERT INTO users(user_id,email,name,password_hash,password_salt,role,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (u, f"{u}@example.com", u, "x", "", "user", "2026-01-01", "2026-01-01"))
        conn.commit()
    return db
