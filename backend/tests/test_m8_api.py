"""M8 — AtlasBench HTTP API: authentication, validation, the full flow, safe failures,
isolation, rate limiting and the one-run-at-a-time guard."""
from __future__ import annotations

import importlib

import pytest

from backend.knowledge import ids
from backend.tests.bench_fixtures import by_model, llm_factory
from backend.tests.m7_fixtures import (  # noqa: F401  (pytest fixtures + helpers)
    NOW, S1, FakeWorld, _fake_embeddings, engine, evolving_scenario, make_user, repository, solar_claim, svc, world,
)

BASE = "/api/atlasbench"
RUN_ID = "run_" + "0" * 32


@pytest.fixture(autouse=True)
def _fresh_state():
    from backend.api.routes import atlasbench as route
    from backend.auth.rate_limit import reset_rate_limits

    reset_rate_limits()
    route.reset_execution_guard()
    yield
    reset_rate_limits()
    route.reset_execution_guard()


@pytest.fixture()
def app(repository, monkeypatch):
    import main as main_module
    importlib.reload(main_module)
    from backend.api.routes import atlasbench as route
    from backend.auth.security import create_access_token
    from backend.bench.service import BenchService
    from backend.database import ResearchRepository
    from fastapi.testclient import TestClient

    state = {"factory": by_model({"llama-3.3-70b-versatile": "perfect", "llama-3.1-8b-instant": "fabricating"})}
    monkeypatch.setattr(route, "_service", lambda: BenchService(ResearchRepository(), llm_factory=lambda c: state["factory"](c), clock=lambda: NOW))
    login = lambda uid, name: {"Authorization": f"Bearer {create_access_token(uid, f'{name.lower()}@example.com')}"}  # noqa: E731
    ua, ub = make_user(repository, "Alice"), make_user(repository, "Bob")
    with TestClient(main_module.app) as client:
        yield {"c": client, "main": main_module, "route": route, "ua": ua, "ub": ub, "ha": login(ua, "Alice"), "hb": login(ub, "Bob"), "repo": repository, "state": state}


def body(resp, ok=True, status=200):
    assert resp.status_code == status, resp.text
    j = resp.json()
    assert j["success"] is ok, j
    return j["data"]


def new_run(app, who="a", config="preset-baseline", **kw):
    payload = {"config_id": config, **({"suite_id": "core-v1"} if not kw else kw)}
    return body(app["c"].post(f"{BASE}/runs", json=payload, headers=app["h" + who]))["run"]


def run_done(app, who="a", config="preset-baseline", **kw):
    r = new_run(app, who, config, **kw)
    data = body(app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["h" + who]))
    return data["run"]


TASK = {"title": "My own task", "question": "Does the custom sample pass the check?",
        "claims": [{"claim_id": "c1", "text": "The custom sample passes the check every time.", "expected_status": "verified", "supporting_docs": ["d1"]}],
        "corpus": [{"doc_id": "d1", "url": "https://mine.example.com/a", "title": "Mine", "content": "The custom sample passes the check every time it is run."}]}


# ── authentication ───────────────────────────────────────────────────────────
def test_every_atlasbench_endpoint_requires_authentication(app):
    paths = app["main"].app.openapi()["paths"]
    ops = [(m.upper(), p) for p, item in paths.items() if p.startswith(BASE) for m in item if m in ("get", "post", "delete")]
    assert len(ops) >= 17, ops
    for method, path in ops:
        url = (path.replace("{run_id}", RUN_ID).replace("{task_id}", "core-clean-support").replace("{config_id}", "preset-baseline"))
        kw = {"json": {}} if method == "POST" else {}
        q = "?run_ids=" + ",".join([RUN_ID, RUN_ID]) if url.endswith("/compare") else ""
        assert app["c"].request(method, url + q, **kw).status_code == 401, (method, path)
        assert app["c"].request(method, url + q, headers={"Authorization": "Bearer bad.token.value"}, **kw).status_code == 401, (method, path)


# ── reads ────────────────────────────────────────────────────────────────────
def test_overview_tasks_and_configs(app):
    o = body(app["c"].get(f"{BASE}/overview", headers=app["ha"]))
    assert o["suites"][0]["suite_id"] == "core-v1" and len(o["metrics"]) == 6 and o["limits"]["max_compare_runs"] == 4
    tasks = body(app["c"].get(f"{BASE}/tasks", headers=app["ha"]))
    assert tasks["total"] == 8
    t = body(app["c"].get(f"{BASE}/tasks/core-contradiction", headers=app["ha"]))["task"]
    assert t["definition"]["claims"][0]["expected_status"] == "conflicting" and t["builtin"] is True and len(t["task_hash"]) == 64
    assert body(app["c"].get(f"{BASE}/tasks?tag=injection", headers=app["ha"]))["total"] == 1
    assert body(app["c"].get(f"{BASE}/configs", headers=app["ha"]))["total"] == 4


@pytest.mark.parametrize("path,status", [
    ("/tasks/Bad_Id!", 422), ("/tasks/x", 422), ("/runs/not-a-run", 422), (f"/runs/{RUN_ID}/cases/UPPER", 422), ("/tasks?source=other", 422),
    ("/tasks?tag=Bad Tag", 422), ("/runs?status=weird", 422), ("/runs?limit=0", 422), ("/runs?limit=101", 422), ("/runs?offset=-1", 422),
    ("/runs?fingerprint=xyz", 422),
])
def test_malformed_inputs_are_rejected_at_the_boundary(app, path, status):
    assert app["c"].get(BASE + path, headers=app["ha"]).status_code == status


def test_malformed_ids_on_write_paths_are_rejected_too(app):
    for path in ("/configs/Bad Id", "/tasks/Bad_Id!", "/runs/nope"):
        assert app["c"].delete(BASE + path, headers=app["ha"]).status_code == 422
    for path in ("/runs/nope/execute", "/runs/nope/reproduce"):
        assert app["c"].post(BASE + path, headers=app["ha"]).status_code == 422


def test_unknown_objects_are_404(app):
    for path in ("/tasks/core-nonexistent", f"/runs/{RUN_ID}", f"/runs/{RUN_ID}/cases/core-clean-support"):
        assert app["c"].get(BASE + path, headers=app["ha"]).status_code == 404
    assert app["c"].delete(f"{BASE}/configs/preset-baseline", headers=app["ha"]).status_code == 409
    assert app["c"].delete(f"{BASE}/tasks/core-clean-support", headers=app["ha"]).status_code == 409


# ── the full flow ────────────────────────────────────────────────────────────
def test_create_execute_inspect_reproduce_compare_delete(app):
    c, h = app["c"], app["ha"]
    created = body(c.post(f"{BASE}/runs", json={"config_id": "preset-baseline", "suite_id": "core-v1", "label": "baseline"}, headers=h))["run"]
    assert created["status"] == "pending" and created["summary"] is None and created["case_count"] == 8
    data = body(c.post(f"{BASE}/runs/{created['run_id']}/execute", headers=h))
    run = data["run"]
    assert data["ran"] is True and run["status"] == "completed" and run["summary"]["composite"]["mean"] > 0.95 and run["cases_done"] == 8

    detail = body(c.get(f"{BASE}/runs/{run['run_id']}", headers=h))["run"]
    assert len(detail["cases"]) == 8 and detail["fingerprint"] == created["fingerprint"]
    case = body(c.get(f"{BASE}/runs/{run['run_id']}/cases/core-contradiction", headers=h))["case"]
    assert case["detail"]["contradiction_handling"]["detected"] == 1 and case["task"]["claims"] and case["trace"] and case["output"]["calls"]

    again = body(c.post(f"{BASE}/runs/{run['run_id']}/execute", headers=h))
    assert again["ran"] is False and again["run"]["summary"] == run["summary"]

    repro = body(c.post(f"{BASE}/runs/{run['run_id']}/reproduce", headers=h))["run"]
    assert repro["reproduces_run_id"] == run["run_id"] and repro["status"] == "pending" and repro["fingerprint"] == run["fingerprint"]
    body(c.post(f"{BASE}/runs/{repro['run_id']}/execute", headers=h))

    other = run_done(app, config="preset-small-model")
    cmpr = body(c.get(f"{BASE}/compare", params={"run_ids": f"{run['run_id']},{other['run_id']}"}, headers=h))
    assert cmpr["composite"]["best"] == run["run_id"] and cmpr["comparable"] and len(cmpr["common_tasks"]) == 8

    listing = body(c.get(f"{BASE}/runs", headers=h))
    assert listing["total"] == 3 and listing["items"][0]["run_id"] == other["run_id"]
    assert body(c.get(f"{BASE}/runs", params={"status": "completed", "limit": 1}, headers=h))["items"][0]["status"] == "completed"
    body(c.delete(f"{BASE}/runs/{repro['run_id']}", headers=h))
    assert c.get(f"{BASE}/runs/{repro['run_id']}", headers=h).status_code == 404


def test_comparing_a_custom_config_across_models(app):
    cfg = body(app["c"].post(f"{BASE}/configs", json={"name": "Iterative 8B", "model": "llama-3.1-8b-instant", "strategy": "iterative", "top_k": 3}, headers=app["ha"]))["config"]
    assert cfg["config_id"].startswith("cfg_") and cfg["price_known"]
    r = run_done(app, config=cfg["config_id"], task_ids=["core-multi-hop", "core-contradiction"])
    assert r["config"]["strategy"] == "iterative" and r["summary"]["metrics"]["claim_correctness"]["mean"] < 0.9      # the "fabricating" 8B model
    assert body(app["c"].delete(f"{BASE}/configs/{cfg['config_id']}", headers=app["ha"]))["config_id"] == cfg["config_id"]
    assert body(app["c"].get(f"{BASE}/runs/{r['run_id']}", headers=app["ha"]))["run"]["config"]["model"] == "llama-3.1-8b-instant"    # snapshot survives


# ── writes: validation ───────────────────────────────────────────────────────
@pytest.mark.parametrize("payload,status", [
    ({"config_id": "preset-baseline"}, 400),                                                       # neither tasks nor suite
    ({"config_id": "preset-baseline", "suite_id": "core-v1", "task_ids": ["core-clean-support"]}, 400),
    ({"config_id": "preset-baseline", "task_ids": []}, 422),
    ({"config_id": "preset-baseline", "task_ids": ["BAD ID"]}, 422),
    ({"config_id": "preset-baseline", "suite_id": "core-v1", "surprise": 1}, 422),
    ({"config_id": "preset-baseline", "task_ids": ["core-clean-support"] * 26}, 422),
    ({"config_id": "preset-nope", "suite_id": "core-v1"}, 404),
    ({"config_id": "preset-baseline", "suite_id": "core-v9"}, 404),
    ({"config_id": "preset-baseline", "task_ids": ["core-ghost"]}, 404),
    ({"config_id": "preset-baseline", "suite_id": "core-v1", "label": "x" * 121}, 422),
])
def test_run_creation_validation(app, payload, status):
    assert app["c"].post(f"{BASE}/runs", json=payload, headers=app["ha"]).status_code == status


@pytest.mark.parametrize("payload,status", [
    ({"name": "x", "model": "gpt-9-ultra"}, 400),                                  # not on the allowlist
    ({"name": "x", "model": "llama-3.3-70b-versatile", "temperature": 2}, 422),
    ({"name": "x", "model": "llama-3.3-70b-versatile", "strategy": "router"}, 422),
    ({"name": "x", "model": "llama-3.3-70b-versatile", "top_k": 50}, 422),
    ({"name": "", "model": "llama-3.3-70b-versatile"}, 422),
    ({"name": "x", "model": "llama-3.3-70b-versatile", "api_key": "sk-123"}, 422),
])
def test_config_validation(app, payload, status):
    assert app["c"].post(f"{BASE}/configs", json=payload, headers=app["ha"]).status_code == status


def test_duplicate_config_names_conflict(app):
    ok = {"name": "dup", "model": "llama-3.3-70b-versatile"}
    body(app["c"].post(f"{BASE}/configs", json=ok, headers=app["ha"]))
    assert app["c"].post(f"{BASE}/configs", json=ok, headers=app["ha"]).status_code == 409
    body(app["c"].post(f"{BASE}/configs", json=ok, headers=app["hb"]))                 # a different user may reuse the name


def test_custom_task_creation_and_validation(app):
    t = body(app["c"].post(f"{BASE}/tasks", json=TASK, headers=app["ha"]))["task"]
    assert t["task_id"].startswith("tsk_") and t["gold_quality"] == "curated" and t["builtin"] is False
    assert app["c"].post(f"{BASE}/tasks", json={**TASK, "task_id": "core-hijack"}, headers=app["ha"]).status_code == 422
    assert app["c"].post(f"{BASE}/tasks", json={**TASK, "gold_quality": "derived"}, headers=app["ha"]).status_code == 422
    bad = {**TASK, "claims": [{**TASK["claims"][0], "supporting_docs": ["zzz"]}]}
    r = app["c"].post(f"{BASE}/tasks", json=bad, headers=app["ha"])
    assert r.status_code == 400 and "unknown documents" in r.json()["detail"]
    huge = {**TASK, "corpus": [{**TASK["corpus"][0], "content": "x" * 7000}]}
    assert app["c"].post(f"{BASE}/tasks", json=huge, headers=app["ha"]).status_code == 422
    r = run_done(app, task_ids=[t["task_id"]])
    assert r["status"] == "completed" and r["case_count"] == 1 and r["summary"]["metrics"]["claim_correctness"]["n"] == 1


@pytest.mark.parametrize("q", ["", "run_x", "a,b", f"{RUN_ID}", ",".join([RUN_ID, "nope"])])
def test_compare_query_validation(app, q):
    assert app["c"].get(f"{BASE}/compare", params={"run_ids": q} if q else {}, headers=app["ha"]).status_code in (400, 422)


def test_compare_needs_two_to_four_completed_runs(app):
    a = run_done(app)
    pending = new_run(app)
    assert app["c"].get(f"{BASE}/compare", params={"run_ids": f"{a['run_id']},{pending['run_id']}"}, headers=app["ha"]).status_code == 409
    assert app["c"].get(f"{BASE}/compare", params={"run_ids": f"{a['run_id']},{a['run_id']}"}, headers=app["ha"]).status_code == 400
    five = ",".join(f"run_{i:032x}" for i in range(5))
    assert app["c"].get(f"{BASE}/compare", params={"run_ids": five}, headers=app["ha"]).status_code == 400


# ── isolation ────────────────────────────────────────────────────────────────
def test_another_users_objects_look_exactly_like_missing_ones(app):
    c = app["c"]
    run = run_done(app)
    task = body(c.post(f"{BASE}/tasks", json=TASK, headers=app["ha"]))["task"]
    cfg = body(c.post(f"{BASE}/configs", json={"name": "private", "model": "llama-3.1-8b-instant"}, headers=app["ha"]))["config"]
    rid = run["run_id"]
    for method, path in (("get", f"/runs/{rid}"), ("get", f"/runs/{rid}/cases/core-clean-support"), ("post", f"/runs/{rid}/execute"),
                         ("post", f"/runs/{rid}/reproduce"), ("delete", f"/runs/{rid}"), ("get", f"/tasks/{task['task_id']}"),
                         ("delete", f"/tasks/{task['task_id']}"), ("delete", f"/configs/{cfg['config_id']}")):
        mine = getattr(c, method)(BASE + path, headers=app["hb"])
        ghost = getattr(c, method)(BASE + path.replace(rid, RUN_ID).replace(task["task_id"], "tsk_" + "f" * 32).replace(cfg["config_id"], "cfg_" + "f" * 32), headers=app["hb"])
        assert mine.status_code == ghost.status_code == 404 and mine.json() == ghost.json(), (method, path)
    assert body(c.get(f"{BASE}/runs", headers=app["hb"]))["total"] == 0
    assert task["task_id"] not in {t["task_id"] for t in body(c.get(f"{BASE}/tasks", headers=app["hb"]))["items"]}
    assert c.post(f"{BASE}/runs", json={"config_id": cfg["config_id"], "suite_id": "core-v1"}, headers=app["hb"]).status_code == 404
    assert c.post(f"{BASE}/runs", json={"config_id": "preset-baseline", "task_ids": [task["task_id"]]}, headers=app["hb"]).status_code == 404
    absent_rid = rid[:-1] + ("1" if rid[-1] != "1" else "2")
    assert c.get(f"{BASE}/compare", params={"run_ids": f"{rid},{absent_rid}"}, headers=app["hb"]).status_code == 404
    assert body(c.get(f"{BASE}/runs/{rid}", headers=app["ha"]))["run"]["status"] == "completed"       # and the owner is untouched


# ── execution guards ─────────────────────────────────────────────────────────
def test_execute_is_rate_limited_per_user(app):
    r = new_run(app)
    url = f"{BASE}/runs/{r['run_id']}/execute"
    for _ in range(6):
        assert app["c"].post(url, headers=app["ha"]).status_code == 200
    limited = app["c"].post(url, headers=app["ha"])
    assert limited.status_code == 429
    other = new_run(app, "b")
    assert app["c"].post(f"{BASE}/runs/{other['run_id']}/execute", headers=app["hb"]).status_code == 200        # per user, not global


def test_only_one_run_executes_at_a_time_per_account(app):
    r = new_run(app)
    app["route"]._active.add(app["ua"])
    resp = app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["ha"])
    assert resp.status_code == 409 and "already executing" in resp.json()["detail"]
    app["route"].reset_execution_guard()
    body(app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["ha"]))
    assert app["ua"] not in app["route"]._active                                                                # released afterwards


def test_the_guard_is_released_even_when_execution_blows_up(app, monkeypatch):
    from backend.bench.service import BenchService

    r = new_run(app)
    monkeypatch.setattr(BenchService, "execute_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["ha"]).status_code == 500
    assert app["ua"] not in app["route"]._active


def test_a_run_that_does_not_complete_reports_success_false_and_leaks_nothing(app):
    secret = "sk-live-SUPERSECRET-token"

    def broken(config):
        raise RuntimeError(f"auth failed for key {secret}")

    app["state"]["factory"] = broken
    r = new_run(app)
    resp = app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["ha"])
    data = body(resp, ok=False)
    assert data["run"]["status"] == "failed" and data["run"]["error_type"] == "model_unavailable" and data["run"]["retryable"] is True
    assert secret not in resp.text and secret not in app["c"].get(f"{BASE}/runs/{r['run_id']}", headers=app["ha"]).text
    app["state"]["factory"] = llm_factory("perfect")
    assert body(app["c"].post(f"{BASE}/runs/{r['run_id']}/execute", headers=app["ha"]))["run"]["status"] == "completed"


def test_unexpected_errors_are_sanitised(app, monkeypatch):
    from backend.bench.service import BenchService

    from types import SimpleNamespace

    monkeypatch.setattr("backend.core.errors.get_settings", lambda: SimpleNamespace(app_env="production"))   # development deliberately shows detail
    monkeypatch.setattr(BenchService, "overview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db password=hunter2 at /srv/secret.db")))
    resp = app["c"].get(f"{BASE}/overview", headers=app["ha"])
    assert resp.status_code == 500 and "hunter2" not in resp.text and "/srv" not in resp.text and "try again" in resp.text


def test_a_running_run_cannot_be_deleted_over_http(app):
    r = new_run(app)
    from backend.bench.repository import BenchRepository
    from backend.database import ResearchRepository

    BenchRepository(ResearchRepository().db).begin_run(app["ua"], r["run_id"], NOW)
    assert app["c"].delete(f"{BASE}/runs/{r['run_id']}", headers=app["ha"]).status_code == 409


def test_responses_never_contain_credentials_or_other_users_ids(app):
    run = run_done(app)
    case = app["c"].get(f"{BASE}/runs/{run['run_id']}/cases/core-clean-support", headers=app["ha"]).text
    for needle in (app["ub"], "GROQ_API_KEY", "TAVILY", "Bearer ", "password"):
        assert needle not in case


# ── Knowledge Map integration ────────────────────────────────────────────────
def test_a_knowledge_map_claim_can_be_frozen_into_a_task_over_http(app, svc, repository):
    evolving_scenario(repository, app["ua"])
    cid, inv = solar_claim(svc, app["ua"])
    payload = {"investigation_id": inv, "claim_id": cid, "include_challenges": False}
    first = body(app["c"].post(f"{BASE}/tasks/from-claim", json=payload, headers=app["ha"]))
    assert first["created"] is True and first["task"]["gold_quality"] == "derived" and first["task"]["definition"]["provenance"]["claim_id"] == cid
    second = app["c"].post(f"{BASE}/tasks/from-claim", json=payload, headers=app["ha"])
    assert second.status_code == 200 and second.json()["data"]["created"] is False and second.json()["data"]["task"]["task_id"] == first["task"]["task_id"]
    r = run_done(app, config="preset-full-corpus", task_ids=[first["task"]["task_id"]])
    assert r["status"] == "completed"
    # Bob cannot freeze Alice's claim; malformed ids are rejected at the boundary
    assert app["c"].post(f"{BASE}/tasks/from-claim", json=payload, headers=app["hb"]).status_code == 404
    assert app["c"].post(f"{BASE}/tasks/from-claim", json={**payload, "claim_id": "nope"}, headers=app["ha"]).status_code == 422
    assert app["c"].post(f"{BASE}/tasks/from-claim", json={**payload, "claim_id": ids.claim_id(app["ua"], "Never claimed")}, headers=app["ha"]).status_code == 404
