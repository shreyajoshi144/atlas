"""M7 — Challenge HTTP API: authentication, validation, flow, safe failures,
isolation, rate limiting, Knowledge Map integration, and the UI↔API contract."""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from backend.challenges.repository import ChallengeRepository
from backend.knowledge import ids
from backend.knowledge.repository import KnowledgeRepository
from backend.tests.m7_fixtures import (  # noqa: F401  (pytest fixtures + helpers)
    NOW, S1, FakeWorld, _fake_embeddings, engine, evolving_scenario, make_user, repository, svc, world,
)

BASE, KM = "/api/challenges", "/api/knowledge-map"
NEW_A, NEW_B = "https://news.example.io/a", "https://other.example.io/b"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from backend.auth.rate_limit import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture()
def app(repository, engine, monkeypatch):
    """Real app + real signed tokens; only the engine's external boundaries are fake."""
    import main as main_module
    importlib.reload(main_module)
    from backend.api.routes import challenges as route
    from backend.auth.security import create_access_token
    from backend.challenges.service import ChallengeService
    from backend.database import ResearchRepository
    from fastapi.testclient import TestClient

    monkeypatch.setattr(route, "_service", lambda: ChallengeService(ResearchRepository(), engine=engine, clock=lambda: NOW))
    login = lambda uid, name: {"Authorization": f"Bearer {create_access_token(uid, f'{name.lower()}@example.com')}"}  # noqa: E731
    ua, ub = make_user(repository, "Alice"), make_user(repository, "Bob")
    ra, rb = evolving_scenario(repository, ua), evolving_scenario(repository, ub)
    with TestClient(main_module.app) as client:
        yield {"c": client, "main": main_module, "ua": ua, "ub": ub, "ra": ra, "rb": rb, "ha": login(ua, "Alice"), "hb": login(ub, "Bob"), "repo": repository}


def body(resp, ok=True, status=200):
    assert resp.status_code == status, resp.text
    j = resp.json()
    assert j["success"] is ok, j
    return j["data"]


def create(app, who="a", text="Solar capacity grew in Europe", ctype="counter_evidence"):
    u, r, h = app["u" + who], app["r" + who], app["h" + who]
    inv = r["r1"] if text == "Tidal pilot launched" else r["r2"]
    return app["c"].post(BASE, json={"investigation_id": inv, "claim_id": ids.claim_id(u, text), "challenge_type": ctype}, headers=h)


# ── authentication ───────────────────────────────────────────────────────────
def test_every_challenge_endpoint_requires_authentication(app):
    paths = app["main"].app.openapi()["paths"]
    ops = [(m.upper(), p) for p, item in paths.items() if p.startswith(BASE) for m in item if m in ("get", "post")]
    assert len(ops) >= 5, ops
    cid = "chl_" + "0" * 32
    for method, path in ops:
        url = path.replace("{challenge_id}", cid)
        kw = {"json": {"investigation_id": "report_x", "claim_id": "clm_" + "0" * 32, "challenge_type": "counter_evidence"}} if method == "POST" and path == BASE else {}
        assert app["c"].request(method, url, **kw).status_code == 401, (method, path)
        assert app["c"].request(method, url, headers={"Authorization": "Bearer bad.token.value"}, **kw).status_code == 401, (method, path)


# ── the full flow ────────────────────────────────────────────────────────────
def test_create_execute_get_list_inspect_over_http(app, world):
    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4)
    c, h = app["c"], app["ha"]
    made = body(create(app))
    assert made["created"] is True and made["challenge"]["status"] == "pending" and made["challenge"]["result"] is None
    cid = made["challenge"]["challenge_id"]

    ran = body(c.post(f"{BASE}/{cid}/execute", headers=h))
    assert ran["ran"] is True and ran["challenge"]["status"] == "completed" and ran["challenge"]["result"] == "contradicted"
    assert body(c.get(f"{BASE}/{cid}", headers=h))["challenge"] == ran["challenge"]
    listing = body(c.get(BASE, params={"claim_id": ids.claim_id(app["ua"], "Solar capacity grew in Europe")}, headers=h))
    assert listing["total"] == 1 and listing["items"][0]["challenge_id"] == cid

    d = body(c.get(f"{BASE}/{cid}/inspect", headers=h))
    assert [s["step"] for s in d["trail"]] == ["claim", "evidence", "challenge", "counter_evidence", "result", "updated_interpretation"]
    assert {e["role"] for e in d["evidence"]} == {"counter", "supporting"} and all(e["provenance"]["retrieved_source"] for e in d["evidence"])
    assert d["interpretation"]["label"] == "contradicted" and d["attempts"][0]["status"] == "completed" and d["trace"]


def test_create_and_execute_are_idempotent_over_http(app, world):
    world.add(NEW_A, "support", 0.7)
    c, h = app["c"], app["ha"]
    first, second = body(create(app)), body(create(app))
    assert (first["created"], second["created"]) == (True, False) and first["challenge"]["challenge_id"] == second["challenge"]["challenge_id"]
    cid = first["challenge"]["challenge_id"]
    body(c.post(f"{BASE}/{cid}/execute", headers=h))
    calls = dict(world.calls)
    again = body(c.post(f"{BASE}/{cid}/execute", headers=h))
    assert again["ran"] is False and world.calls == calls


# ── failure + retry, with no error leakage ───────────────────────────────────
def test_failed_execution_is_reported_honestly_and_can_be_retried(app, world):
    world.add(NEW_A, "contradict", 0.9)
    world.search_fail = 2
    c, h = app["c"], app["ha"]
    cid = body(create(app))["challenge"]["challenge_id"]
    failed = c.post(f"{BASE}/{cid}/execute", headers=h)
    data = body(failed, ok=False)                                            # HTTP 200, but success=false
    assert data["challenge"]["status"] == "failed" and data["challenge"]["result"] is None and data["challenge"]["retryable"] is True
    assert failed.json()["message"] == "Challenge failed"
    everything = " ".join(r.text for r in [failed, c.get(f"{BASE}/{cid}", headers=h), c.get(f"{BASE}/{cid}/inspect", headers=h), c.get(BASE, headers=h)])
    assert "search backend down" not in everything and "RuntimeError" not in everything and "Traceback" not in everything
    assert data["challenge"]["error_type"] == "ChallengeToolFailure"
    retried = body(c.post(f"{BASE}/{cid}/execute", headers=h))
    assert retried["challenge"]["status"] == "completed" and retried["challenge"]["attempt_count"] == 2


def test_retries_are_capped_with_a_409(app, world):
    world.search_fail = 99
    c, h = app["c"], app["ha"]
    cid = body(create(app))["challenge"]["challenge_id"]
    for _ in range(3):
        body(c.post(f"{BASE}/{cid}/execute", headers=h), ok=False)
    r = c.post(f"{BASE}/{cid}/execute", headers=h)
    assert r.status_code == 409 and "create a new challenge" in r.json()["detail"]


def test_challenging_a_failed_investigation_is_a_409(app):
    KnowledgeRepository(app["repo"].db).record_failure(app["ua"], app["ra"]["r2"], RuntimeError("secret db detail"))
    r = create(app)
    assert r.status_code == 409 and "failed" in r.json()["detail"] and "secret db detail" not in r.text


# ── validation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [
    {"challenge_type": "run_arbitrary_agent"}, {"challenge_type": ""}, {"claim_id": "not-an-id"}, {"claim_id": "ent_" + "0" * 32},
    {"investigation_id": "x"}, {"investigation_id": "r" * 121}, {"extra_field": "nope"},
])
def test_create_rejects_bad_input_with_422(app, payload):
    ok = {"investigation_id": app["ra"]["r2"], "claim_id": ids.claim_id(app["ua"], "Hydro is stable"), "challenge_type": "counter_evidence"}
    assert app["c"].post(BASE, json={**ok, **payload}, headers=app["ha"]).status_code == 422


def test_create_rejects_missing_fields_and_malformed_ids_and_filters(app):
    c, h = app["c"], app["ha"]
    assert c.post(BASE, json={}, headers=h).status_code == 422
    for path in ["/not-an-id", "/chl_XYZ", "/clm_" + "0" * 32, "/chl_" + "0" * 32 + "/execute/extra"]:
        assert c.get(BASE + path, headers=h).status_code in (404, 422), path
    for bad in ["/not-an-id/inspect", "/chl_xyz/inspect"]:
        assert c.get(BASE + bad, headers=h).status_code == 422
    for qs in ["status=bogus", "result=bogus", "challenge_type=bogus", "claim_id=nope", "limit=101", "limit=0", "offset=-1"]:
        assert c.get(f"{BASE}?{qs}", headers=h).status_code == 422, qs
    assert c.post(f"{BASE}/not-an-id/execute", headers=h).status_code == 422


# ── isolation ────────────────────────────────────────────────────────────────
def test_foreign_and_missing_challenges_are_indistinguishable_404s_and_nothing_runs(app, world):
    world.add(NEW_A, "contradict", 0.9)
    c = app["c"]
    cid = body(create(app))["challenge"]["challenge_id"]                      # Alice's, still pending
    missing = "chl_" + "a" * 32
    calls = dict(world.calls)
    for suffix, method in [("", "get"), ("/inspect", "get"), ("/execute", "post")]:
        foreign = getattr(c, method)(f"{BASE}/{cid}{suffix}", headers=app["hb"])
        absent = getattr(c, method)(f"{BASE}/{missing}{suffix}", headers=app["hb"])
        assert foreign.status_code == absent.status_code == 404 and foreign.json() == absent.json(), suffix
    assert world.calls == calls
    assert body(c.get(f"{BASE}/{cid}", headers=app["ha"]))["challenge"]["status"] == "pending"   # untouched


def test_creating_against_someone_elses_claim_looks_like_a_missing_claim(app):
    c = app["c"]
    alice_claim = {"investigation_id": app["ra"]["r2"], "claim_id": ids.claim_id(app["ua"], "Hydro is stable"), "challenge_type": "counter_evidence"}
    ghost = {**alice_claim, "claim_id": "clm_" + "a" * 32}
    a, b = c.post(BASE, json=alice_claim, headers=app["hb"]), c.post(BASE, json=ghost, headers=app["hb"])
    assert a.status_code == b.status_code == 404 and a.json() == b.json()
    mixed = c.post(BASE, json={**alice_claim, "claim_id": ids.claim_id(app["ub"], "Hydro is stable")}, headers=app["hb"])  # Bob's claim, Alice's investigation
    assert mixed.status_code == 404
    assert body(c.get(BASE, headers=app["hb"]))["total"] == 0


def test_lists_only_show_the_callers_challenges(app, world):
    world.add(NEW_A, "support", 0.7)
    body(create(app, "a")); body(create(app, "b")); body(create(app, "b", text="Hydro is stable"))
    assert body(app["c"].get(BASE, headers=app["ha"]))["total"] == 1 and body(app["c"].get(BASE, headers=app["hb"]))["total"] == 2
    assert body(app["c"].get(BASE, params={"investigation_id": app["rb"]["r2"]}, headers=app["ha"]))["total"] == 0


# ── rate limiting ────────────────────────────────────────────────────────────
def test_execute_is_rate_limited_per_user(app, world):
    world.add(NEW_A, "support", 0.7)
    c = app["c"]
    cid = body(create(app, "a"))["challenge"]["challenge_id"]
    bob = body(create(app, "b"))["challenge"]["challenge_id"]
    assert [c.post(f"{BASE}/{cid}/execute", headers=app["ha"]).status_code for _ in range(10)] == [200] * 10
    assert c.post(f"{BASE}/{cid}/execute", headers=app["ha"]).status_code == 429
    assert c.post(f"{BASE}/{bob}/execute", headers=app["hb"]).status_code == 200      # per-user, not global


# ── Knowledge Map integration over HTTP ──────────────────────────────────────
def test_knowledge_map_shows_the_challenge_trail_and_graph_links(app, world):
    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4)
    c, h, ua = app["c"], app["ha"], app["ua"]
    cid = body(create(app))["challenge"]["challenge_id"]
    body(c.post(f"{BASE}/{cid}/execute", headers=h))
    claim_id = ids.claim_id(ua, "Solar capacity grew in Europe")

    d = body(c.get(f"{KM}/claims/{claim_id}", headers=h))
    assert d["interpretation"]["label"] == "contradicted" and d["confidence"] == 0.9            # original untouched
    assert d["challenges_detail"][0]["challenge_id"] == cid and [s["step"] for s in d["challenge_trail"]][-1] == "updated_interpretation"
    assert body(c.get(f"{KM}/summary", headers=h))["challenges"]["by_result"] == {"contradicted": 1}
    g = body(c.get(f"{KM}/graph", params={"focus": claim_id, "depth": 2, "limit": 100}, headers=h))
    kinds = {n["type"] for n in g["nodes"]}
    assert {"claim", "challenge", "counter"} <= kinds and {"challenged_by", "counter_evidence"} <= {e["type"] for e in g["edges"]}
    counter = next(n["id"] for n in g["nodes"] if n["type"] == "counter")
    assert body(c.get(f"{KM}/graph", params={"focus": cid}, headers=h))["focus"] == cid
    assert body(c.get(f"{KM}/graph", params={"focus": counter}, headers=h))["focus"] == counter
    # Bob can neither see nor focus them
    assert c.get(f"{KM}/graph", params={"focus": cid}, headers=app["hb"]).status_code == 404
    assert c.get(f"{KM}/graph", params={"focus": counter}, headers=app["hb"]).status_code == 404
    assert body(c.get(f"{KM}/summary", headers=app["hb"]))["challenges"]["total"] == 0


def test_get_endpoints_never_write(app, world):
    world.add(NEW_A, "contradict", 0.9)
    c, h = app["c"], app["ha"]
    cid = body(create(app))["challenge"]["challenge_id"]
    body(c.post(f"{BASE}/{cid}/execute", headers=h))
    def counts():
        with app["repo"].db.get_connection() as conn:
            return {t: conn.execute(f"SELECT COUNT(*), MAX(updated_at) FROM {t}" if t == "challenges" else f"SELECT COUNT(*) FROM {t}").fetchone()[:] for t in
                    ("challenges", "challenge_attempts", "challenge_sources", "challenge_evidence")}
    before = counts()
    for url in (BASE, f"{BASE}/{cid}", f"{BASE}/{cid}/inspect", f"{KM}/summary", f"{KM}/graph?limit=50"):
        body(c.get(url, headers=h))
    assert counts() == before
    assert ChallengeRepository(app["repo"].db).integrity_problems() == []


# ── UI ↔ API contract ────────────────────────────────────────────────────────
def test_ui_calls_only_existing_challenge_endpoints_and_reads_real_fields(app, world):
    js = (ROOT / "frontend" / "js" / "knowledge.js").read_text(encoding="utf-8")
    paths = set(app["main"].app.openapi()["paths"])
    assert BASE in paths and f"{BASE}/{{challenge_id}}/execute" in paths and f"{BASE}/{{challenge_id}}/inspect" in paths
    assert "apiPost('/challenges'" in js and "'/execute'" in js and "'/inspect'" in js

    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4)
    c, h = app["c"], app["ha"]
    cid = body(create(app))["challenge"]["challenge_id"]
    body(c.post(f"{BASE}/{cid}/execute", headers=h))
    d = body(c.get(f"{BASE}/{cid}/inspect", headers=h))
    assert {"challenge_id", "challenge_type", "status", "result", "error_type", "retryable", "evidence", "attempts", "trail", "sources",
            "dropped_unverified_evidence"} <= set(d)
    assert {"role", "passage", "strength", "source"} <= set(d["evidence"][0]) and {"url", "domain", "independent", "publish_date"} <= set(d["evidence"][0]["source"])
    assert {"attempt_no", "status", "started_at", "finished_at", "error_type"} <= set(d["attempts"][0])
    assert {"step"} <= set(d["trail"][0]) and {"queries", "challenge_type"} <= set(d["trail"][2]) and {"value", "rationale"} <= set(d["trail"][4])
    assert {"label", "summary", "adjusted_confidence"} <= set(d["trail"][5])
    claim = body(c.get(f"{KM}/claims/{ids.claim_id(app['ua'], 'Solar capacity grew in Europe')}", headers=h))
    assert {"challenges_detail", "interpretation", "investigation", "graph_state", "challenge_trail"} <= set(claim)
    assert {"label", "summary", "original_status", "original_confidence", "adjusted_confidence"} <= set(claim["interpretation"])
    assert {"challenge_id", "challenge_type", "status", "result", "completed_at", "counter_evidence_count"} <= set(claim["challenges_detail"][0])
    assert "report_id" in claim["investigation"]
    # the claim LIST (used by the claim rows) carries the interpretation the badge reads
    listed = body(c.get(f"{KM}/claims", params={"query": "solar"}, headers=h))["items"][0]
    assert {"label", "summary"} <= set(listed["interpretation"]) and {"total", "latest_result"} <= set(listed["challenges"])
