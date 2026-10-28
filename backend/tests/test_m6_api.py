"""M6 — Knowledge Map HTTP API: authentication, authorisation/isolation, validation,
explicit knowledge states, read-only behaviour, and route-guard regressions."""
from __future__ import annotations

import importlib
import json
import re

import pytest

from backend.knowledge import ids
from backend.knowledge.repository import KnowledgeRepository
from backend.tests.m6_fixtures import _fake_embeddings, repository, svc  # noqa: F401  (pytest fixtures)
from backend.tests.m6_fixtures import S1, evolving_scenario, make_user

BASE = "/api/knowledge-map"


@pytest.fixture()
def api(repository):
    """Real app + real signed tokens (no auth override): authentication is exercised."""
    import main as main_module
    importlib.reload(main_module)
    from backend.auth.security import create_access_token
    from fastapi.testclient import TestClient

    def login(user_id, name):
        return {"Authorization": f"Bearer {create_access_token(user_id, f'{name.lower()}@example.com')}"}

    with TestClient(main_module.app) as client:
        yield client, login, main_module


@pytest.fixture()
def world(repository, api):
    client, login, _ = api
    ua, ub = make_user(repository, "Alice"), make_user(repository, "Bob")
    ra, rb = evolving_scenario(repository, ua), evolving_scenario(repository, ub)
    return {"ua": ua, "ub": ub, "ra": ra, "rb": rb, "ha": login(ua, "Alice"), "hb": login(ub, "Bob"), "client": client, "repo": repository}


def data(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    return body["data"]


# ── authentication ───────────────────────────────────────────────────────────
def _knowledge_map_operations(main_module):
    paths = main_module.app.openapi()["paths"]
    ops = [(m.upper(), p) for p, item in paths.items() if p.startswith(BASE) for m in item if m in ("get", "post")]
    assert len(ops) >= 12, ops  # guards against enumerating nothing
    return ops


def _concrete(path):
    return re.sub(r"\{(\w+)\}", lambda m: {"entity_id": "ent_" + "0" * 32, "claim_id": "clm_" + "0" * 32, "evidence_id": "evd_" + "0" * 32,
                                          "source_id": "src_" + "0" * 32, "report_id": "report_x"}[m.group(1)], path)


def test_every_knowledge_map_endpoint_requires_authentication(api):
    client, _, main_module = api
    for method, path in _knowledge_map_operations(main_module):
        url = _concrete(path) + ("?base=a1b&target=c2d" if path.endswith("/changes") else "")
        assert client.request(method, url).status_code == 401, (method, path)
        assert client.request(method, url, headers={"Authorization": "Bearer not.a.token"}).status_code == 401, (method, path)


# ── happy path through HTTP ──────────────────────────────────────────────────
def test_end_to_end_drill_down_over_http(world):
    c, h, ua = world["client"], world["ha"], world["ua"]
    s = data(c.get(f"{BASE}/summary", headers=h))
    assert s["map_is_complete"] is True and s["investigations"]["complete"] == 2

    ents = data(c.get(f"{BASE}/entities", params={"query": "europe"}, headers=h))
    eid = ents["items"][0]["entity_id"]
    claim = next(x for x in data(c.get(f"{BASE}/entities/{eid}", headers=h))["claims"] if x["text"].startswith("Solar"))
    detail = data(c.get(f"{BASE}/claims/{claim['claim_id']}", headers=h))
    assert {"status", "confidence", "evidence", "history", "stale", "last_verified_at", "graph_state"} <= set(detail)
    ev = detail["evidence"]["supporting"][0]
    evd = data(c.get(f"{BASE}/evidence/{ev['evidence_id']}", headers=h))
    src = data(c.get(f"{BASE}/sources/{evd['source']['source_id']}", headers=h))
    assert src["freshness"]["label"] in {"fresh", "aging", "old", "unknown"} and src["url"] == ev["source"]["url"]


def test_graph_and_changes_over_http(world):
    c, h, ra = world["client"], world["ha"], world["ra"]
    g = data(c.get(f"{BASE}/graph", params={"limit": 300}, headers=h))
    node_ids = {n["id"] for n in g["nodes"]}
    assert node_ids and all(e["source"] in node_ids and e["target"] in node_ids for e in g["edges"])
    d = data(c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": ra["r2"]}, headers=h))
    assert d["summary"]["new"] == 1 and d["summary"]["contradicted"] == 1 and d["as_of"].startswith("2026-06-01")


# ── authorisation: foreign and missing look identical ────────────────────────
def test_foreign_and_nonexistent_ids_are_indistinguishable_404s(world):
    c, ua, hb = world["client"], world["ua"], world["hb"]
    foreign = {
        f"{BASE}/claims/{ids.claim_id(ua, 'Hydro is stable')}": f"{BASE}/claims/{'clm_' + 'a' * 32}",
        f"{BASE}/entities/{ids.entity_id(ua, 'Europe')}": f"{BASE}/entities/{'ent_' + 'a' * 32}",
        f"{BASE}/sources/{ids.source_id(ua, S1)}": f"{BASE}/sources/{'src_' + 'a' * 32}",
        f"{BASE}/graph?focus={ids.claim_id(ua, 'Hydro is stable')}": f"{BASE}/graph?focus={'clm_' + 'a' * 32}",
    }
    for real_foreign, missing in foreign.items():
        a, b = c.get(real_foreign, headers=hb), c.get(missing, headers=hb)
        assert a.status_code == b.status_code == 404 and a.json() == b.json(), real_foreign


def test_users_only_see_their_own_data_over_http(world):
    c = world["client"]
    a = {x["claim_id"] for x in data(c.get(f"{BASE}/claims", params={"limit": 100}, headers=world["ha"]))["items"]}
    b = {x["claim_id"] for x in data(c.get(f"{BASE}/claims", params={"limit": 100}, headers=world["hb"]))["items"]}
    assert a and b and not (a & b)
    own = {i["report_id"] for i in data(c.get(f"{BASE}/investigations", headers=world["hb"]))["items"]}
    assert own == set(world["rb"].values())


def test_cannot_compare_or_rebuild_another_users_investigation(world):
    c, hb, ra = world["client"], world["hb"], world["ra"]
    assert c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": ra["r2"]}, headers=hb).status_code == 404
    strip = lambda s: {k: v for k, v in s.items() if k != "generated_at"}  # timestamp differs per call
    before = strip(data(c.get(f"{BASE}/summary", headers=world["ha"])))
    assert c.post(f"{BASE}/investigations/{ra['r1']}/rebuild", headers=hb).status_code == 404
    assert strip(data(c.get(f"{BASE}/summary", headers=world["ha"]))) == before


# ── validation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", [
    "/claims/not-an-id", "/claims/clm_XYZ", "/entities/clm_" + "0" * 32, "/sources/ent_" + "0" * 32, "/evidence/'; DROP TABLE x;--",
    "/claims?status=bogus", "/claims?limit=101", "/claims?limit=0", "/claims?offset=-1", "/sources?freshness=ancient",
    "/graph?depth=9", "/graph?limit=2", "/graph?focus=notanid", "/entities?query=" + "x" * 201, "/claims?entity_id=nope",
])
def test_malformed_input_is_rejected_with_422_never_500(world, path):
    assert world["client"].get(BASE + path, headers=world["ha"]).status_code == 422


def test_sql_metacharacters_in_search_are_inert(world):
    c, h = world["client"], world["ha"]
    for q in ["' OR 1=1 --", "%", "_", "\\", "'; DROP TABLE knowledge_claims;--"]:
        assert data(c.get(f"{BASE}/claims", params={"query": q}, headers=h))["items"] == []
    assert data(c.get(f"{BASE}/summary", headers=h))["counts"]["claims"] == 6  # still intact


def test_changes_input_errors(world):
    c, h, ra = world["client"], world["ha"], world["ra"]
    assert c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": ra["r1"]}, headers=h).status_code == 400
    assert c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": "report_missing"}, headers=h).status_code == 404
    assert c.get(f"{BASE}/changes", params={"base": ra["r1"]}, headers=h).status_code == 422


# ── explicit knowledge state over HTTP ───────────────────────────────────────
def test_failed_state_is_visible_blocks_comparison_and_can_be_rebuilt(world):
    c, h, ua, ra, repo = world["client"], world["ha"], world["ua"], world["ra"], world["repo"]
    KnowledgeRepository(repo.db).record_failure(ua, ra["r2"], RuntimeError("secret internal: SELECT * FROM x"))

    s = data(c.get(f"{BASE}/summary", headers=h))
    assert s["map_is_complete"] is False and s["investigations"]["failed"] == 1
    inv = {i["report_id"]: i for i in data(c.get(f"{BASE}/investigations", headers=h))["items"]}
    assert inv[ra["r2"]]["knowledge_state"] == "failed" and inv[ra["r2"]]["comparable"] is False

    blocked = c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": ra["r2"]}, headers=h)
    assert blocked.status_code == 409 and ra["r2"] in blocked.json()["detail"] and "failed" in blocked.json()["detail"]

    everything = " ".join(r.text for r in [
        c.get(f"{BASE}/summary", headers=h), c.get(f"{BASE}/investigations", headers=h), blocked,
        c.get(f"{BASE}/claims", params={"limit": 100}, headers=h), c.get(f"{BASE}/graph", params={"limit": 300}, headers=h)])
    assert "secret internal" not in everything and "SELECT" not in everything
    assert any(x["graph_state"] == "failed" for x in data(c.get(f"{BASE}/claims", params={"limit": 100}, headers=h))["items"])

    rebuilt = data(c.post(f"{BASE}/investigations/{ra['r2']}/rebuild", headers=h))
    assert rebuilt["status"] == "complete"
    assert data(c.get(f"{BASE}/summary", headers=h))["map_is_complete"] is True
    assert c.get(f"{BASE}/changes", params={"base": ra["r1"], "target": ra["r2"]}, headers=h).status_code == 200


def test_other_users_failure_does_not_block_my_comparison(world):
    c, ua, ra, rb = world["client"], world["ua"], world["ra"], world["rb"]
    KnowledgeRepository(world["repo"].db).record_failure(ua, ra["r2"], "boom")
    assert c.get(f"{BASE}/changes", params={"base": rb["r1"], "target": rb["r2"]}, headers=world["hb"]).status_code == 200


# ── read-only + idempotent ───────────────────────────────────────────────────
def _row_counts(repo):
    tables = ["knowledge_sources", "knowledge_evidence", "knowledge_claims", "knowledge_entities", "knowledge_report_sources",
              "knowledge_claim_observations", "knowledge_claim_evidence", "knowledge_claim_entities",
              "knowledge_evidence_entities", "knowledge_investigation_status", "reports", "report_chunks"]
    with repo.db.get_connection() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_get_endpoints_never_write(world):
    c, h, ra, repo = world["client"], world["ha"], world["ra"], world["repo"]
    before = _row_counts(repo)
    for url in [f"{BASE}/summary", f"{BASE}/investigations", f"{BASE}/entities", f"{BASE}/claims", f"{BASE}/sources",
                f"{BASE}/graph", f"{BASE}/changes?base={ra['r1']}&target={ra['r2']}"]:
        data(c.get(url, headers=h))
    assert _row_counts(repo) == before


def test_rebuild_is_idempotent_over_http(world):
    c, h, ra, repo = world["client"], world["ha"], world["ra"], world["repo"]
    c.post(f"{BASE}/investigations/{ra['r1']}/rebuild", headers=h)
    first = _row_counts(repo)
    again = data(c.post(f"{BASE}/investigations/{ra['r1']}/rebuild", headers=h))
    assert again["status"] == "complete" and _row_counts(repo) == first
    assert KnowledgeRepository(repo.db).integrity_problems() == []


# ── regression: the route guard must actually see the routes ─────────────────
def test_openapi_exposes_the_knowledge_map_and_no_planner_or_tool_routes(api):
    _, _, main_module = api
    paths = set(main_module.app.openapi()["paths"])
    assert len(paths) > 25 and any(p.startswith(BASE) for p in paths)
    assert not any("plan" in p or "tool" in p for p in paths), paths
    for existing in ("/api/research/run", "/api/history/reports", "/api/chat/ask", "/health"):
        assert existing in paths or any(p.startswith(existing.rsplit("/", 1)[0]) for p in paths)
