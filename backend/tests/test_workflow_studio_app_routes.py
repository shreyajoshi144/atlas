"""Regression coverage for Workflow Studio routes on the actual FastAPI app."""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_all_workflow_studio_endpoints_are_registered_and_auth_protected():
    import main as main_module

    main_module = importlib.reload(main_module)
    paths = main_module.app.openapi()["paths"]
    expected = {
        ("get", "/api/workflows/templates"),
        ("get", "/api/workflows"),
        ("post", "/api/workflows"),
        ("get", "/api/workflows/{workflow_id}"),
        ("put", "/api/workflows/{workflow_id}"),
        ("post", "/api/workflows/{workflow_id}/duplicate"),
        ("delete", "/api/workflows/{workflow_id}"),
        ("post", "/api/workflows/{workflow_id}/execute"),
        ("get", "/api/workflows/{workflow_id}/runs"),
        ("get", "/api/workflows/runs/{run_id}"),
    }
    registered = {
        (method, path)
        for path, operations in paths.items()
        for method in operations
        if method in {"get", "post", "put", "delete", "patch"}
    }
    assert expected <= registered, f"Missing Workflow Studio routes: {expected - registered}"

    # Exercise the registered ASGI app, not only the router module. Requests
    # without a bearer token must be rejected by the app-level protection.
    with TestClient(main_module.app) as client:
        for method, path in expected:
            concrete = path.replace("{workflow_id}", "wf_" + "0" * 32).replace("{run_id}", "wfr_" + "0" * 32)
            kwargs = {"json": {"name": "Workflow", "steps": [{"type": "input_text"}, {"type": "uppercase"}]}} if method in {"post", "put"} else {}
            if method == "post" and concrete.endswith("/execute"):
                kwargs["headers"] = {"Idempotency-Key": "route-test-0001"}
            response = client.request(method.upper(), concrete, **kwargs)
            assert response.status_code in {401, 403}, (method, concrete, response.status_code, response.text)


def test_registered_workflow_api_persists_execution_history_and_enforces_owner(tmp_path, monkeypatch):
    import main as main_module
    from backend.auth.dependencies import get_current_user
    from backend.api.routes import workflows as workflow_routes
    from backend.database.sqlite import SQLiteManager
    from backend.workflows.service import WorkflowService

    main_module = importlib.reload(main_module)
    db = SQLiteManager(str(tmp_path / "workflow-api.sqlite"))
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO users(user_id) VALUES (?)", [("alice",), ("bob",)])
        conn.commit()
    service = WorkflowService(db)
    service.initialize()
    monkeypatch.setattr(workflow_routes, "_service", lambda: service)
    principal = {"user_id": "alice"}
    main_module.app.dependency_overrides[get_current_user] = lambda: principal
    try:
        with TestClient(main_module.app) as client:
            base = "/api/workflows"
            created = client.post(base, json={"name": "Uppercase", "steps": [
                {"type": "input_text"}, {"type": "uppercase"}
            ]})
            assert created.status_code == 200, created.text
            wf = created.json()["data"]["workflow"]
            wid = wf["workflow_id"]
            assert client.get(base).json()["data"]["items"][0]["workflow_id"] == wid
            assert client.get(f"{base}/{wid}").json()["data"]["workflow"]["name"] == "Uppercase"

            headers = {"Idempotency-Key": "api-execute-0001"}
            first = client.post(f"{base}/{wid}/execute", json={"text": "hello"}, headers=headers)
            assert first.status_code == 200, first.text
            run = first.json()["data"]["run"]
            assert run["status"] == "completed" and run["output"]["text"] == "HELLO"
            repeated = client.post(f"{base}/{wid}/execute", json={"text": "hello"}, headers=headers)
            assert repeated.json()["data"]["created"] is False
            assert repeated.json()["data"]["run"]["run_id"] == run["run_id"]
            assert client.get(f"{base}/{wid}/runs").json()["data"]["items"][0]["run_id"] == run["run_id"]
            assert client.get(f"{base}/runs/{run['run_id']}").json()["data"]["run"]["steps"][-1]["output"]["text"] == "HELLO"

            bad = client.post(base, json={"name": "Invalid", "steps": [
                {"type": "input_text"}, {"type": "run_shell"}
            ]})
            assert bad.status_code == 400, bad.text
            failing_wf_response = client.post(base, json={"name": "Require needle", "steps": [
                {"type": "input_text"}, {"type": "require_contains", "text": "needle"}
            ]})
            assert failing_wf_response.status_code == 200, failing_wf_response.text
            failing_wid = failing_wf_response.json()["data"]["workflow"]["workflow_id"]
            failed = client.post(f"{base}/{failing_wid}/execute", json={"text": "haystack"}, headers={"Idempotency-Key": "api-failure-0001"})
            assert failed.status_code == 200
            failed_run = failed.json()["data"]["run"]
            assert failed.json()["success"] is False and failed_run["status"] == "failed"
            assert failed_run["steps"][-1]["error"] == "Required text was not found."

            principal["user_id"] = "bob"
            assert client.get(f"{base}/{wid}").status_code == 404
            assert client.get(f"{base}/runs/{run['run_id']}").status_code == 404
    finally:
        main_module.app.dependency_overrides.pop(get_current_user, None)
