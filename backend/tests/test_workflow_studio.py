from __future__ import annotations

import sqlite3
import pytest
from backend.database.sqlite import SQLiteManager
from backend.workflows.service import WorkflowService, WorkflowConflict, WorkflowNotFound


@pytest.fixture
def service(tmp_path):
    db = SQLiteManager(str(tmp_path / "workflow.sqlite"))
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users(user_id) VALUES ('alice'),('bob')")
        conn.commit()
    svc = WorkflowService(db)
    svc.initialize()
    return svc


def test_template_create_execute_persist_steps_and_history(service):
    template = service.templates()["items"][0]
    wf = service.create("alice", {"name": "Cleanup", "steps": template["steps"]})
    run, created = service.execute("alice", wf["workflow_id"], "  hello   world  ", "request-0001")
    assert created and run["status"] == "completed"
    assert run["output"]["text"] == "hello world\nProcessed by Atlas Workflow Studio."
    assert [s["status"] for s in run["steps"]] == ["completed"] * 3
    again, created_again = service.execute("alice", wf["workflow_id"], "  hello   world  ", "request-0001")
    assert not created_again and again["run_id"] == run["run_id"]
    assert len(service.history("alice", wf["workflow_id"])["items"]) == 1


def test_idempotency_key_cannot_be_reused_for_different_input(service):
    wf = service.create("alice", {"name": "Upper", "steps": [{"type": "input_text"}, {"type": "uppercase"}]})
    service.execute("alice", wf["workflow_id"], "first", "request-0002")
    with pytest.raises(WorkflowConflict):
        service.execute("alice", wf["workflow_id"], "second", "request-0002")


def test_validation_rejects_unsupported_step_and_wrong_first_step(service):
    with pytest.raises(ValueError, match="unsupported step"):
        service.create("alice", {"name": "Bad", "steps": [{"type": "input_text"}, {"type": "run_shell"}]})
    with pytest.raises(ValueError, match="first step"):
        service.create("alice", {"name": "Bad", "steps": [{"type": "uppercase"}]})


def test_failed_condition_is_persisted_with_step_outcome(service):
    wf = service.create("alice", {"name": "Check", "steps": [{"type": "input_text"}, {"type": "require_contains", "text": "needle"}]})
    run, _ = service.execute("alice", wf["workflow_id"], "haystack", "request-0003")
    assert run["status"] == "failed"
    assert run["steps"][-1]["status"] == "failed"
    assert run["steps"][-1]["error"] == "Required text was not found."


def test_workflows_and_runs_are_user_scoped(service):
    wf = service.create("alice", {"name": "Private", "steps": [{"type": "input_text"}, {"type": "uppercase"}]})
    run, _ = service.execute("alice", wf["workflow_id"], "secret", "request-0004")
    with pytest.raises(WorkflowNotFound):
        service.get("bob", wf["workflow_id"])
    with pytest.raises(WorkflowNotFound):
        service.get_run("bob", run["run_id"])
    assert service.list("bob")["items"] == []


def test_run_preserves_versioned_definition_and_step_configuration_after_edit(service):
    wf = service.create("alice", {"name": "Append A", "steps": [
        {"type": "input_text", "name": "Load"},
        {"type": "append_text", "name": "Append configured suffix", "text": "-A"},
    ]})
    run, _ = service.execute("alice", wf["workflow_id"], "value", "snapshot-0001")
    service.update("alice", wf["workflow_id"], {"name": "Append B", "steps": [
        {"type": "input_text", "name": "Load"},
        {"type": "append_text", "name": "Append configured suffix", "text": "-B"},
    ]})
    historical = service.get_run("alice", run["run_id"])
    assert historical["output"]["text"] == "value-A"
    assert historical["snapshot_version"] == 1
    assert historical["definition_snapshot"]["definition"]["steps"][1]["text"] == "-A"
    assert historical["steps"][1]["config"]["text"] == "-A"


def test_duplicate_delete_and_cross_user_ownership(service):
    wf = service.create("alice", {"name": "Original", "steps": [{"type": "input_text"}, {"type": "uppercase"}]})
    duplicate = service.duplicate("alice", wf["workflow_id"])
    assert duplicate["name"] == "Original (copy)"
    assert duplicate["definition"]["steps"] == wf["definition"]["steps"]
    with pytest.raises(WorkflowNotFound):
        service.duplicate("bob", wf["workflow_id"])
    with pytest.raises(WorkflowNotFound):
        service.delete("bob", wf["workflow_id"])
    assert service.delete("alice", duplicate["workflow_id"])["deleted"] is True
    with pytest.raises(WorkflowNotFound):
        service.get("alice", duplicate["workflow_id"])


def test_stale_interrupted_run_is_terminalized_without_reexecution(service):
    wf = service.create("alice", {"name": "Recover", "steps": [{"type": "input_text"}, {"type": "uppercase"}]})
    run, _ = service.execute("alice", wf["workflow_id"], "secret", "recover-0001")
    with service.db.get_connection() as conn:
        conn.execute("UPDATE workflow_runs SET status='running', output_json=NULL, finished_at=NULL, created_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (run["run_id"],))
        conn.commit()
    recovered = service.get_run("alice", run["run_id"])
    assert recovered["status"] == "failed"
    assert "Execution interrupted" in recovered["error"]
    again, created = service.execute("alice", wf["workflow_id"], "secret", "recover-0001")
    assert not created and again["status"] == "failed"


def test_initialize_migrates_existing_workflow_run_tables(tmp_path):
    db = SQLiteManager(str(tmp_path / "legacy-workflow.sqlite"))
    with db.get_connection() as conn:
        conn.executescript("""
        CREATE TABLE users (user_id TEXT PRIMARY KEY);
        INSERT INTO users(user_id) VALUES ('alice');
        CREATE TABLE workflow_definitions (
          workflow_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
          definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE TABLE workflow_runs (
          run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
          input_json TEXT NOT NULL, output_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
          UNIQUE(user_id, workflow_id, idempotency_key),
          FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
          FOREIGN KEY(workflow_id) REFERENCES workflow_definitions(workflow_id) ON DELETE CASCADE
        );
        CREATE TABLE workflow_run_steps (
          run_id TEXT NOT NULL, step_index INTEGER NOT NULL, step_name TEXT NOT NULL,
          step_type TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL,
          output_json TEXT, error TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
          PRIMARY KEY(run_id, step_index), FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
        );
        """)
        conn.commit()
    WorkflowService(db).initialize()
    with db.get_connection() as conn:
        run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_runs)")}
        step_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_run_steps)")}
    assert {"definition_snapshot_json", "snapshot_version"} <= run_cols
    assert "config_json" in step_cols
