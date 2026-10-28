from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from backend.database.sqlite import SQLiteManager

MAX_STEPS = 10
MAX_TEXT = 10_000
INTERRUPTED_RUN_TIMEOUT_SECONDS = 120
RUN_SNAPSHOT_VERSION = 1
TEMPLATES = {
    "text-cleanup": {
        "template_id": "text-cleanup", "name": "Text cleanup",
        "description": "Set input text, normalize it, then append a closing note.",
        "steps": [
            {"type": "input_text", "name": "Load input"},
            {"type": "normalize_whitespace", "name": "Normalize whitespace"},
            {"type": "append_text", "name": "Append note", "text": "\nProcessed by Atlas Workflow Studio."},
        ],
    },
    "text-validation": {
        "template_id": "text-validation", "name": "Text validation",
        "description": "Validate that input contains a required phrase.",
        "steps": [
            {"type": "input_text", "name": "Load input"},
            {"type": "require_contains", "name": "Check required phrase", "text": "TODO"},
        ],
    },
}
ALLOWED_STEPS = {"input_text", "normalize_whitespace", "append_text", "uppercase", "require_contains"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class WorkflowError(Exception):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowConflict(WorkflowError):
    pass


class WorkflowService:
    def __init__(self, db: SQLiteManager | None = None):
        self.db = db or SQLiteManager()

    def initialize(self):
        with self.db.get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_definitions (
              workflow_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
              definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_def_user ON workflow_definitions(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS workflow_runs (
              run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
              input_json TEXT NOT NULL, output_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
              definition_snapshot_json TEXT, snapshot_version INTEGER NOT NULL DEFAULT 1,
              UNIQUE(user_id, workflow_id, idempotency_key),
              FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
              FOREIGN KEY(workflow_id) REFERENCES workflow_definitions(workflow_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_user ON workflow_runs(user_id, created_at);
            CREATE TABLE IF NOT EXISTS workflow_run_steps (
              run_id TEXT NOT NULL, step_index INTEGER NOT NULL, step_name TEXT NOT NULL,
              step_type TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed','failed','skipped')),
              input_json TEXT NOT NULL, output_json TEXT, error TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
              config_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY(run_id, step_index), FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
            );
            """)
            # CREATE TABLE IF NOT EXISTS does not migrate existing installations.
            run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()}
            if "definition_snapshot_json" not in run_cols:
                conn.execute("ALTER TABLE workflow_runs ADD COLUMN definition_snapshot_json TEXT")
            if "snapshot_version" not in run_cols:
                conn.execute("ALTER TABLE workflow_runs ADD COLUMN snapshot_version INTEGER NOT NULL DEFAULT 1")
            step_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_run_steps)").fetchall()}
            if "config_json" not in step_cols:
                conn.execute("ALTER TABLE workflow_run_steps ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}' ")
            conn.commit()

    @staticmethod
    def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
        name = str(definition.get("name", "")).strip()
        steps = definition.get("steps")
        if not 2 <= len(name) <= 100:
            raise ValueError("Workflow name must be 2–100 characters.")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError(f"A workflow must contain 1–{MAX_STEPS} steps.")
        normalized = []
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict) or raw.get("type") not in ALLOWED_STEPS:
                raise ValueError(f"Step {i + 1}: unsupported step type.")
            step = {"type": raw["type"], "name": str(raw.get("name") or raw["type"].replace("_", " ").title())[:80]}
            if raw["type"] in {"append_text", "require_contains"}:
                value = raw.get("text")
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError(f"Step {i + 1}: text must be a string of at most 2000 characters.")
                step["text"] = value
            normalized.append(step)
        if normalized[0]["type"] != "input_text":
            raise ValueError("The first step must be input_text.")
        return {"name": name, "steps": normalized}

    def templates(self):
        return {"items": list(TEMPLATES.values())}

    def create(self, user_id: str, definition: dict[str, Any], template_id: str | None = None):
        if template_id:
            if template_id not in TEMPLATES:
                raise ValueError("Unknown workflow template.")
            base = TEMPLATES[template_id]
            definition = {"name": definition.get("name") or base["name"], "steps": definition.get("steps", base["steps"])}
        valid = self.validate_definition(definition)
        wid, now = _id("wf"), _now()
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO workflow_definitions VALUES (?,?,?,?,?,?)", (wid, user_id, valid["name"], json.dumps(valid), now, now))
            conn.commit()
        return self.get(user_id, wid)

    def list(self, user_id: str):
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT workflow_id,name,definition_json,created_at,updated_at FROM workflow_definitions WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return {"items": [self._definition_row(r) for r in rows]}

    @staticmethod
    def _definition_row(row):
        return {"workflow_id": row["workflow_id"], "name": row["name"], "definition": json.loads(row["definition_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get(self, user_id: str, workflow_id: str):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT workflow_id,name,definition_json,created_at,updated_at FROM workflow_definitions WHERE workflow_id=? AND user_id=?", (workflow_id, user_id)).fetchone()
        if row is None:
            raise WorkflowNotFound(workflow_id)
        return self._definition_row(row)

    def update(self, user_id: str, workflow_id: str, definition: dict[str, Any]):
        valid = self.validate_definition(definition)
        with self.db.get_connection() as conn:
            cur = conn.execute("UPDATE workflow_definitions SET name=?,definition_json=?,updated_at=? WHERE workflow_id=? AND user_id=?", (valid["name"], json.dumps(valid), _now(), workflow_id, user_id))
            conn.commit()
        if cur.rowcount != 1:
            raise WorkflowNotFound(workflow_id)
        return self.get(user_id, workflow_id)

    def duplicate(self, user_id: str, workflow_id: str, name: str | None = None):
        original = self.get(user_id, workflow_id)
        definition = original["definition"]
        return self.create(user_id, {"name": (name or f"{definition['name']} (copy)")[:100], "steps": definition["steps"]})

    def delete(self, user_id: str, workflow_id: str):
        # Ownership is part of the DELETE predicate; foreign resources look absent.
        with self.db.get_connection() as conn:
            cur = conn.execute("DELETE FROM workflow_definitions WHERE workflow_id=? AND user_id=?", (workflow_id, user_id))
            conn.commit()
        if cur.rowcount != 1:
            raise WorkflowNotFound(workflow_id)
        return {"workflow_id": workflow_id, "deleted": True}

    def execute(self, user_id: str, workflow_id: str, text: str, idempotency_key: str):
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError(f"input_text must be a string of at most {MAX_TEXT} characters.")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 100:
            raise ValueError("Idempotency-Key must contain 8–100 characters.")
        definition_row = self.get(user_id, workflow_id)
        definition = definition_row["definition"]
        snapshot = {"version": RUN_SNAPSHOT_VERSION, "workflow_id": workflow_id,
                    "workflow_name": definition["name"], "definition": definition}
        run_id, now = _id("wfr"), _now()
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT run_id,input_json FROM workflow_runs WHERE user_id=? AND workflow_id=? AND idempotency_key=?", (user_id, workflow_id, idempotency_key)).fetchone()
            if existing:
                if json.loads(existing["input_json"]) != {"text": text}:
                    conn.rollback()
                    raise WorkflowConflict("This idempotency key was already used with a different input.")
                conn.commit()
                return self.get_run(user_id, existing["run_id"]), False
            conn.execute("""INSERT INTO workflow_runs
                (run_id,user_id,workflow_id,idempotency_key,status,input_json,output_json,error,created_at,finished_at,definition_snapshot_json,snapshot_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, user_id, workflow_id, idempotency_key, "running", json.dumps({"text": text}), None, None, now, None, json.dumps(snapshot), RUN_SNAPSHOT_VERSION))
            conn.commit()
        current = text
        overall = "completed"
        error = None
        with self.db.get_connection() as conn:
            for i, step in enumerate(definition["steps"]):
                started = _now(); before = current; step_status = "completed"; step_error = None
                try:
                    kind = step["type"]
                    if kind == "input_text": current = text
                    elif kind == "normalize_whitespace": current = " ".join(current.split())
                    elif kind == "append_text": current += step["text"]
                    elif kind == "uppercase": current = current.upper()
                    elif kind == "require_contains":
                        if step["text"] not in current:
                            raise ValueError("Required text was not found.")
                    if len(current) > MAX_TEXT:
                        raise ValueError("Step output exceeds the 10000 character limit.")
                except Exception as exc:
                    overall, error, step_status, step_error = "failed", str(exc), "failed", str(exc)
                conn.execute("""INSERT INTO workflow_run_steps
                    (run_id,step_index,step_name,step_type,status,input_json,output_json,error,started_at,finished_at,config_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, i, step["name"], step["type"], step_status, json.dumps({"text": before}), json.dumps({"text": current}) if step_status == "completed" else None, step_error, started, _now(), json.dumps(step)))
                if overall == "failed": break
            conn.execute("UPDATE workflow_runs SET status=?,output_json=?,error=?,finished_at=? WHERE run_id=? AND user_id=? AND status='running'", (overall, json.dumps({"text": current}) if overall == "completed" else None, error, _now(), run_id, user_id))
            conn.commit()
        return self.get_run(user_id, run_id), True

    def _fail_if_interrupted(self, user_id: str, run_id: str, created_at: str):
        """Terminalize stale deterministic runs; never replay work implicitly.

        A running record older than the lease window is treated as interrupted.
        This is a terminal-failure policy, not automatic re-execution, so adding
        future side-effecting steps cannot silently repeat their effects.
        """
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return
        if datetime.now(timezone.utc) - created <= timedelta(seconds=INTERRUPTED_RUN_TIMEOUT_SECONDS):
            return
        with self.db.get_connection() as conn:
            conn.execute("""UPDATE workflow_runs SET status='failed', error=?, finished_at=?
                WHERE run_id=? AND user_id=? AND status='running'""",
                ("Execution interrupted: run remained in running state beyond the recovery window. Re-submit with a new idempotency key.", _now(), run_id, user_id))
            conn.commit()

    def get_run(self, user_id: str, run_id: str):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT r.* FROM workflow_runs r WHERE r.run_id=? AND r.user_id=?", (run_id, user_id)).fetchone()
            if row is None:
                raise WorkflowNotFound(run_id)
            status, created_at = row["status"], row["created_at"]
        if status == "running":
            self._fail_if_interrupted(user_id, run_id, created_at)
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT r.* FROM workflow_runs r WHERE r.run_id=? AND r.user_id=?", (run_id, user_id)).fetchone()
            steps = conn.execute("SELECT * FROM workflow_run_steps WHERE run_id=? ORDER BY step_index", (run_id,)).fetchall()
        return {"run_id": row["run_id"], "workflow_id": row["workflow_id"], "status": row["status"],
                "input": json.loads(row["input_json"]), "output": json.loads(row["output_json"]) if row["output_json"] else None,
                "error": row["error"], "created_at": row["created_at"], "finished_at": row["finished_at"],
                "definition_snapshot": json.loads(row["definition_snapshot_json"]) if row["definition_snapshot_json"] else None,
                "snapshot_version": row["snapshot_version"],
                "steps": [{"step_index": s["step_index"], "name": s["step_name"], "type": s["step_type"],
                           "config": json.loads(s["config_json"] or "{}"), "status": s["status"],
                           "input": json.loads(s["input_json"]), "output": json.loads(s["output_json"]) if s["output_json"] else None,
                           "error": s["error"]} for s in steps]}

    def history(self, user_id: str, workflow_id: str):
        self.get(user_id, workflow_id)
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT run_id FROM workflow_runs WHERE user_id=? AND workflow_id=? ORDER BY created_at DESC LIMIT 100", (user_id, workflow_id)).fetchall()
        return {"items": [self.get_run(user_id, r["run_id"]) for r in rows]}
