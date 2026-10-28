"""M9 — routing over HTTP, end to end: auth, validation, isolation, AtlasBench-driven autopilot,
real research-route and LangGraph integration, usage/cost reporting."""
from __future__ import annotations

import importlib
import json
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langchain_core.messages import HumanMessage

from backend.core.llm import get_chat_llm, invoke_with_retry
from backend.routing import repository as repo_mod
from backend.routing.context import current_scope, in_current_context, routing_scope
from backend.routing.router import ModelRouter, get_routed_llm
from backend.tests.bench_fixtures import by_model
from backend.tests.m6_fixtures import NOW, _fake_embeddings, make_user, repository  # noqa: F401  (pytest fixtures)
from backend.tests.test_m9_router import FakeClient, Http, ok

BASE = "/api/routing"
BIG, SMALL, MID = "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b"
GHOST = "pol_" + "0" * 32


@pytest.fixture(autouse=True)
def _fresh_state():
    from backend.auth.rate_limit import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture()
def world(repository, monkeypatch):
    """A running app + a test router whose provider clients are scripted. Also installed as THE process router,
    so ``get_chat_llm()`` (what every agent uses) goes through it."""
    import main as main_module
    importlib.reload(main_module)
    from backend.api.routes import routing as route
    from backend.auth.security import create_access_token
    from backend.database import ResearchRepository
    from backend.routing import router as router_mod
    from backend.routing.service import RoutingService
    from fastapi.testclient import TestClient

    calls: list[str] = []
    scripts: dict[str, list] = {}
    router = ModelRouter(repository_factory=lambda: ResearchRepository(), client_factory=lambda m: FakeClient(m, scripts.get(m, [ok()]), calls),
                         clock=lambda: NOW, default_model=BIG)
    monkeypatch.setattr(router_mod, "get_router", lambda: router)
    for fn in (get_routed_llm, get_chat_llm):
        fn.cache_clear()
    monkeypatch.setattr(route, "_service", lambda: RoutingService(ResearchRepository(), router=router, clock=lambda: NOW))
    token = lambda uid, name: {"Authorization": f"Bearer {create_access_token(uid, f'{name.lower()}@example.com')}"}  # noqa: E731
    ua, ub = make_user(repository, "Alice"), make_user(repository, "Bob")
    with TestClient(main_module.app) as client:
        yield {"c": client, "main": main_module, "ua": ua, "ub": ub, "ha": token(ua, "Alice"), "hb": token(ub, "Bob"), "repo": repository,
               "router": router, "calls": calls, "scripts": scripts}
    for fn in (get_routed_llm, get_chat_llm):
        fn.cache_clear()


def script(w, model, steps):
    """Change what a provider does from now on (the router caches clients, so drop the cached one)."""
    w["scripts"][model] = steps
    w["router"]._clients.pop(model, None)


def body(resp, status=200):
    assert resp.status_code == status, resp.text
    j = resp.json()
    assert j["success"] is True, j
    return j["data"]


def make_policy(w, who="a", **kw):
    payload = {"name": f"policy {len(w['c'].get(f'{BASE}/policies', headers=w['h' + who]).json()['data']['items'])}", "model": BIG, **kw}
    return body(w["c"].post(f"{BASE}/policies", json=payload, headers=w["h" + who]))["policy"]


def activate(w, policy_id, who="a"):
    return body(w["c"].put(f"{BASE}/active", json={"policy_id": policy_id}, headers=w["h" + who]))


def seed_benchmarks(w, models=None, user="ua"):
    """Real AtlasBench runs (M8 service, scripted models) so autopilot evidence comes from actual stored results."""
    from backend.bench.service import BenchService

    mapping = models or {BIG: "perfect", MID: "perfect", SMALL: "fabricating"}
    bs = BenchService(w["repo"], llm_factory=by_model(mapping), clock=lambda: NOW)
    runs = {}
    for model in mapping:
        cfg = bs.create_config(w[user], {"name": f"cfg {model}", "model": model, "strategy": "single_pass"})
        run = bs.create_run(w[user], cfg["config_id"], suite_id="core-v1")
        view, _ = bs.execute_run(w[user], run["run_id"])
        assert view["status"] == "completed"
        runs[model] = view
    return runs


# ── auth ─────────────────────────────────────────────────────────────────────
def test_every_routing_endpoint_requires_authentication(world):
    paths = world["main"].app.openapi()["paths"]
    ops = [(m.upper(), p) for p, item in paths.items() if p.startswith(BASE) for m in item if m in ("get", "post", "put", "delete")]
    assert len(ops) >= 8, ops
    for method, path in ops:
        url = path.replace("{policy_id}", GHOST)
        kw = {"json": {}} if method in ("POST", "PUT") else {}
        assert world["c"].request(method, url, **kw).status_code == 401, (method, path)
        assert world["c"].request(method, url, headers={"Authorization": "Bearer bad.token.value"}, **kw).status_code == 401, (method, path)


# ── policies ─────────────────────────────────────────────────────────────────
def test_overview_lists_allowed_models_prices_limits_and_no_evidence_yet(world):
    o = body(world["c"].get(f"{BASE}/overview", headers=world["ha"]))
    assert o["default_model"] == BIG and o["active_policy"] is None and o["fallback"]["never_during_benchmarks"] is True
    big = next(m for m in o["models"] if m["model"] == BIG)
    assert big["price_in"] == 0.59 and big["evidence"] is None and o["limits"]["max_policies"] == 20 and "rate_limited" in o["fallback"]["categories"]


def test_policy_lifecycle_create_activate_deactivate_delete(world):
    c, h = world["c"], world["ha"]
    p = make_policy(world, model=MID, mode="explicit")
    assert p["policy_id"].startswith("pol_") and p["active"] is False and len(p["policy_hash"]) == 64
    listing = activate(world, p["policy_id"])
    assert listing["active_policy_id"] == p["policy_id"] and listing["items"][0]["active"] is True
    assert body(c.get(f"{BASE}/overview", headers=h))["active_policy"]["policy_id"] == p["policy_id"]
    assert c.delete(f"{BASE}/policies/{p['policy_id']}", headers=h).status_code == 409           # must deactivate first
    assert activate(world, None)["active_policy_id"] is None
    body(c.delete(f"{BASE}/policies/{p['policy_id']}", headers=h))
    assert body(c.get(f"{BASE}/policies", headers=h))["total"] == 0


@pytest.mark.parametrize("payload,status", [
    # allowlist violations are caught by the service → 400
    ({"name": "x", "model": "gpt-9-ultra"}, 400),
    ({"name": "x", "model": BIG, "fallback_enabled": True, "fallback_models": ["evil/model"]}, 400),
    ({"name": "x", "model": BIG, "mode": "autopilot", "candidate_models": ["evil/model"]}, 400),
    # malformed or incoherent policies are rejected by the request schema → 422
    ({"name": "x", "model": BIG, "fallback_enabled": True}, 422),                                    # fallback with nowhere to go
    ({"name": "x", "model": BIG, "fallback_models": [BIG]}, 422),
    ({"name": "x", "model": BIG, "min_quality": 2}, 422),
    ({"name": "x", "model": BIG, "mode": "router"}, 422),
    ({"name": "x", "model": BIG, "max_latency_ms": 5}, 422),
    ({"name": "x", "model": BIG, "api_key": "sk-123"}, 422),
    ({"model": BIG}, 422),
])
def test_policy_validation(world, payload, status):
    assert world["c"].post(f"{BASE}/policies", json=payload, headers=world["ha"]).status_code == status


def test_duplicate_names_conflict_per_user_and_the_cap_is_enforced(world, monkeypatch):
    c = world["c"]
    make_policy(world, name="dup")
    assert c.post(f"{BASE}/policies", json={"name": "dup", "model": BIG}, headers=world["ha"]).status_code == 409
    body(c.post(f"{BASE}/policies", json={"name": "dup", "model": BIG}, headers=world["hb"]))      # another user may reuse the name
    monkeypatch.setattr(repo_mod, "MAX_POLICIES_PER_USER", 2)
    make_policy(world, name="second")
    r = c.post(f"{BASE}/policies", json={"name": "third", "model": BIG}, headers=world["ha"])
    assert r.status_code == 409 and "at most 2" in r.json()["detail"]


def test_malformed_ids_are_rejected_and_unknown_ones_are_404(world):
    c, h = world["c"], world["ha"]
    assert c.delete(f"{BASE}/policies/not-a-policy", headers=h).status_code == 422
    assert c.put(f"{BASE}/active", json={"policy_id": "nope"}, headers=h).status_code == 422
    assert c.delete(f"{BASE}/policies/{GHOST}", headers=h).status_code == 404
    assert c.put(f"{BASE}/active", json={"policy_id": GHOST}, headers=h).status_code == 404
    assert c.put(f"{BASE}/active", json={"policy_id": GHOST, "x": 1}, headers=h).status_code == 422


def test_another_users_policy_looks_exactly_like_a_missing_one(world):
    p = make_policy(world)
    c, hb = world["c"], world["hb"]
    mine = c.delete(f"{BASE}/policies/{p['policy_id']}", headers=hb)
    ghost = c.delete(f"{BASE}/policies/{GHOST}", headers=hb)
    assert mine.status_code == ghost.status_code == 404 and mine.json() == ghost.json()
    assert c.put(f"{BASE}/active", json={"policy_id": p["policy_id"]}, headers=hb).status_code == 404
    assert c.post(f"{BASE}/preview", json={"policy_id": p["policy_id"]}, headers=hb).status_code == 404
    assert body(c.get(f"{BASE}/policies", headers=hb))["items"] == []
    assert body(c.get(f"{BASE}/policies", headers=world["ha"]))["total"] == 1


# ── AtlasBench-driven autopilot ──────────────────────────────────────────────
def test_autopilot_uses_real_atlasbench_results_to_pick_the_cheapest_good_model(world):
    runs = seed_benchmarks(world)
    pol = make_policy(world, mode="autopilot", model=BIG, min_quality=0.9, max_latency_ms=5000, max_cost_per_call_usd=0.05)
    d = body(world["c"].post(f"{BASE}/preview", json={"policy_id": pol["policy_id"], "prompt_tokens": 3000}, headers=world["ha"]))["decision"]
    assert d["requested_model"] == MID and d["reason_code"] == "autopilot_selected" and d["evidence_run_id"] == runs[MID]["run_id"]
    by = {c["model"]: c for c in d["candidates"]}
    assert by[SMALL]["eligible"] is False and "quality" in by[SMALL]["rejected"][0] and by[BIG]["eligible"] is True
    assert by[MID]["est_cost_usd"] < by[BIG]["est_cost_usd"] and by[MID]["evidence"]["quality"] >= 0.9 and by[SMALL]["evidence"]["quality"] < 0.9
    assert "Cheaper but rejected: llama-3.1-8b-instant" in d["reason"]
    assert runs[MID]["summary"]["composite_incl_failures"] == by[MID]["evidence"]["quality"]       # the evidence IS the stored benchmark number


def test_raising_the_quality_bar_changes_the_choice(world):
    seed_benchmarks(world, {BIG: "perfect", MID: "one_sided"})
    lenient = body(world["c"].post(f"{BASE}/preview", json={"policy": {"name": "l", "model": BIG, "mode": "autopilot", "min_quality": 0.5}}, headers=world["ha"]))["decision"]
    strict = body(world["c"].post(f"{BASE}/preview", json={"policy": {"name": "s", "model": BIG, "mode": "autopilot", "min_quality": 0.99}}, headers=world["ha"]))["decision"]
    assert lenient["requested_model"] == MID and strict["requested_model"] == BIG


def test_a_latency_limit_is_enforced_from_benchmark_timings(world):
    runs = seed_benchmarks(world, {BIG: "perfect", MID: "perfect"})
    conn = world["repo"].db.get_connection()
    row = conn.execute("SELECT summary_json FROM bench_runs WHERE run_id = ?", (runs[MID]["run_id"],)).fetchone()
    s = json.loads(row["summary_json"])
    s["performance"]["llm_latency_ms"] = s["performance"]["llm_calls"] * 9000                      # this model took 9 s per call
    conn.execute("UPDATE bench_runs SET summary_json = ? WHERE run_id = ?", (json.dumps(s), runs[MID]["run_id"]))
    conn.commit()
    d = body(world["c"].post(f"{BASE}/preview", json={"policy": {"name": "p", "model": BIG, "mode": "autopilot", "min_quality": 0.5, "max_latency_ms": 4000}}, headers=world["ha"]))["decision"]
    mid = next(c for c in d["candidates"] if c["model"] == MID)
    assert d["requested_model"] == BIG and not mid["eligible"] and "latency 9000 ms/call > limit 4000 ms" in mid["rejected"][0]


def test_a_cost_limit_is_enforced_from_price_and_call_size(world):
    seed_benchmarks(world, {BIG: "perfect", MID: "perfect"})
    pol = {"name": "p", "model": SMALL, "mode": "autopilot", "min_quality": 0.5, "max_cost_per_call_usd": 0.0005}
    small_call = body(world["c"].post(f"{BASE}/preview", json={"policy": pol, "prompt_tokens": 500}, headers=world["ha"]))["decision"]
    huge_call = body(world["c"].post(f"{BASE}/preview", json={"policy": pol, "prompt_tokens": 150_000}, headers=world["ha"]))["decision"]
    assert small_call["requested_model"] == MID and huge_call["reason_code"] == "autopilot_no_qualifying_model" and huge_call["requested_model"] == SMALL


def test_stale_benchmarks_are_ignored(world):
    runs = seed_benchmarks(world, {MID: "perfect"})
    conn = world["repo"].db.get_connection()
    conn.execute("UPDATE bench_runs SET completed_at = '2025-01-01T00:00:00+00:00' WHERE run_id = ?", (runs[MID]["run_id"],))
    conn.commit()
    d = body(world["c"].post(f"{BASE}/preview", json={"policy": {"name": "p", "model": BIG, "mode": "autopilot"}}, headers=world["ha"]))["decision"]
    assert d["reason_code"] == "autopilot_no_evidence" and d["requested_model"] == BIG


def test_one_users_benchmarks_never_inform_another_users_routing(world):
    seed_benchmarks(world, user="ua")
    pol = {"name": "p", "model": BIG, "mode": "autopilot", "min_quality": 0.9}
    alice = body(world["c"].post(f"{BASE}/preview", json={"policy": pol}, headers=world["ha"]))["decision"]
    bob = body(world["c"].post(f"{BASE}/preview", json={"policy": pol}, headers=world["hb"]))["decision"]
    assert alice["reason_code"] == "autopilot_selected" and bob["reason_code"] == "autopilot_no_evidence" and all(c["evidence"] is None for c in bob["candidates"])
    assert all(m["evidence"] is None for m in body(world["c"].get(f"{BASE}/overview", headers=world["hb"]))["models"])
    assert any(m["evidence"] for m in body(world["c"].get(f"{BASE}/overview", headers=world["ha"]))["models"])


def test_preview_sources_and_input_rules(world):
    c, h = world["c"], world["ha"]
    assert body(c.post(f"{BASE}/preview", json={}, headers=h))["source"] == "default"
    p = make_policy(world, model=MID)
    activate(world, p["policy_id"])
    assert body(c.post(f"{BASE}/preview", json={}, headers=h))["source"] == "active"
    saved = body(c.post(f"{BASE}/preview", json={"policy_id": p["policy_id"]}, headers=h))
    assert saved["source"] == "saved" and saved["decision"]["requested_model"] == MID and saved["decision"]["reason_code"] == "explicit"
    assert c.post(f"{BASE}/preview", json={"policy_id": p["policy_id"], "policy": {"name": "x", "model": BIG}}, headers=h).status_code == 400
    assert c.post(f"{BASE}/preview", json={"policy": {"name": "x", "model": "evil"}}, headers=h).status_code == 400
    for bad in ({"prompt_tokens": 0}, {"prompt_tokens": 10**7}, {"surprise": 1}):
        assert c.post(f"{BASE}/preview", json=bad, headers=h).status_code == 422
    assert world["calls"] == []                                                                  # preview never calls a model


def test_preview_is_rate_limited_per_user(world):
    for _ in range(60):
        assert world["c"].post(f"{BASE}/preview", json={}, headers=world["ha"]).status_code == 200
    assert world["c"].post(f"{BASE}/preview", json={}, headers=world["ha"]).status_code == 429
    assert world["c"].post(f"{BASE}/preview", json={}, headers=world["hb"]).status_code == 200


# ── the real production path: research route → graph → agents' get_chat_llm ──
class _S(TypedDict, total=False):
    a: str
    b: str


def real_graph_invoke(state):
    """A genuinely compiled LangGraph with two PARALLEL nodes that call the model exactly the way the agents do."""
    def node(key):
        def run(_):
            return {key: invoke_with_retry(get_chat_llm(), [HumanMessage(content="evidence " * 200)]).content}
        return run

    g = StateGraph(_S)
    g.add_node("a", node("a"))
    g.add_node("b", node("b"))
    g.add_edge(START, "a"); g.add_edge(START, "b"); g.add_edge("a", END); g.add_edge("b", END)
    out = g.compile().invoke({})
    from backend.models.domain_models import ResearchReport, ResearchSession

    return {"session": ResearchSession(topic=state["topic"], status="completed"),
            "final_report": ResearchReport(report_id="report_m9", topic=state["topic"], summary=out["a"] + "|" + out["b"], report_markdown="# s"),
            "ranked_sources": [], "verification_summary": None}


@pytest.fixture()
def research(world, monkeypatch):
    import backend.api.routes.research as research_route

    class Graph:
        def invoke(self, state):
            return real_graph_invoke(state)

    monkeypatch.setattr(research_route, "build_research_graph", lambda: Graph())
    return lambda who="a": world["c"].post("/api/research/run", json={"topic": "Routing integration research topic", "persist_memory": False}, headers=world["h" + who])


def usage(w, who="a", **params):
    return body(w["c"].get(f"{BASE}/usage", params={"limit": 100, **params}, headers=w["h" + who]))


def test_every_model_call_in_a_research_run_is_routed_and_attributed_to_the_requesting_user(world, research):
    p = make_policy(world, model=MID)
    activate(world, p["policy_id"])
    assert research().status_code == 200
    rows = usage(world)["items"]
    assert len(rows) == 2 and {r["actual_model"] for r in rows} == {MID} and {r["purpose"] for r in rows} == {"research"}     # BOTH parallel nodes
    assert len({r["run_ref"] for r in rows}) == 1 and all(r["policy_id"] == p["policy_id"] for r in rows)
    assert world["calls"] == [MID, MID] and usage(world, "b")["total"] == 0


def test_the_research_route_hands_the_scope_to_its_worker_thread(world, research):
    """Without the context hand-off the graph thread would see NO scope and fall through to the unrouted default."""
    assert research().status_code == 200
    assert usage(world)["total"] == 2                                                              # recorded ⇒ the scope reached the graph's threads
    assert current_scope() is None                                                                 # and nothing leaked back into the request context


def test_plain_run_in_executor_loses_the_scope_which_is_why_the_route_uses_the_helper():
    import asyncio

    async def go():
        loop = asyncio.get_running_loop()
        with routing_scope("u", "research"):
            lost = await loop.run_in_executor(None, current_scope)
            kept = await loop.run_in_executor(None, in_current_context(current_scope))
        return lost, kept

    lost, kept = asyncio.run(go())
    assert lost is None and kept is not None and kept.user_id == "u"


def test_autopilot_plus_fallback_through_the_real_route(world, research):
    runs = seed_benchmarks(world)
    p = make_policy(world, mode="autopilot", model=BIG, min_quality=0.9, fallback_enabled=True, fallback_models=[BIG])
    activate(world, p["policy_id"])
    script(world, MID, [Http(503)])                                                               # autopilot's pick is down
    assert research().status_code == 200
    rows = usage(world, fallback_only=True)["items"]
    assert len(rows) == 2 and all(r["requested_model"] == MID and r["actual_model"] == BIG and r["fallback_used"] for r in rows)
    r = rows[0]
    assert r["mode"] == "autopilot" and r["reason_code"] == "autopilot_selected" and r["evidence_run_id"] == runs[MID]["run_id"]
    assert r["fallback_reason"] == f"{MID}: provider error (HTTP 503) → {BIG}" and r["actual_cost_usd"] > 0 and r["total_tokens"] == 1200


def test_a_failing_provider_without_fallback_fails_the_run_visibly(world, research):
    script(world, BIG, [Http(429, "rate_limit_exceeded")])
    import backend.core.llm as llm_mod
    import time as _t
    _orig = _t.sleep
    llm_mod.time.sleep = lambda s: None
    try:
        r = research()
    finally:
        llm_mod.time.sleep = _orig
    assert r.status_code >= 400
    rows = usage(world)["items"]
    assert rows and all(x["status"] == "failed" and x["fallback_used"] is False and x["actual_model"] is None for x in rows)


def test_a_usage_record_exists_even_for_users_who_never_configured_routing(world, research):
    research("b")
    rows = usage(world, "b")["items"]
    assert len(rows) == 2 and {r["actual_model"] for r in rows} == {BIG} and {r["policy_name"] for r in rows} == {"Application default"}


# ── usage & cost reporting ───────────────────────────────────────────────────
def test_usage_listing_filters_paginates_and_is_scoped(world, research):
    p = make_policy(world, model=MID, fallback_enabled=True, fallback_models=[SMALL])
    activate(world, p["policy_id"])
    research()
    script(world, MID, [Http(500)])
    research()
    research("b")
    a = usage(world)
    assert a["total"] == 4 and usage(world, "b")["total"] == 2
    assert usage(world, fallback_only="true")["total"] == 2 and usage(world, model=SMALL)["total"] == 2 and usage(world, purpose="chat")["total"] == 0
    assert usage(world, status="failed")["total"] == 0 and len(usage(world, limit=1, offset=1)["items"]) == 1
    row = a["items"][0]
    for forbidden in ("prompt", "messages", "content", "response", "api_key"):
        assert forbidden not in json.dumps(row).lower().replace("prompt_tokens", "")
    assert body(world["c"].get(f"{BASE}/usage", params={"run_ref": row["run_ref"]}, headers=world["ha"]))["total"] == 2


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"purpose": "weird"}, {"status": "meh"}, {"model": "bad model!"}, {"run_ref": "xyz"}])
def test_usage_query_validation(world, params):
    assert world["c"].get(f"{BASE}/usage", params=params, headers=world["ha"]).status_code == 422


def test_usage_summary_matches_the_rows(world, research):
    p = make_policy(world, model=MID, fallback_enabled=True, fallback_models=[SMALL])
    activate(world, p["policy_id"])
    research()
    script(world, MID, [Http(429)])
    research()
    rows = usage(world)["items"]
    s = body(world["c"].get(f"{BASE}/usage/summary", params={"days": 7}, headers=world["ha"]))
    by = {m["model"]: m for m in s["by_model"]}
    assert s["total"]["calls"] == 4 and s["total"]["fallbacks"] == 2 and s["total"]["fallback_rate"] == 0.5 and set(by) == {MID, SMALL}
    assert by[MID]["calls"] == 2 and by[SMALL]["fallbacks"] == 2 and s["total"]["total_tokens"] == sum(r["total_tokens"] for r in rows)
    assert s["total"]["cost_usd"] == pytest.approx(sum(r["actual_cost_usd"] for r in rows)) and s["total"]["cost_complete"] is True
    assert body(world["c"].get(f"{BASE}/usage/summary", headers=world["hb"]))["total"]["calls"] == 0
    for bad in (0, 366):
        assert world["c"].get(f"{BASE}/usage/summary", params={"days": bad}, headers=world["ha"]).status_code == 422


def test_unexpected_errors_are_sanitised(world, monkeypatch):
    from types import SimpleNamespace
    from backend.routing.service import RoutingService

    monkeypatch.setattr("backend.core.errors.get_settings", lambda: SimpleNamespace(app_env="production"))
    monkeypatch.setattr(RoutingService, "overview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db password=hunter2 at /srv/secret.db")))
    r = world["c"].get(f"{BASE}/overview", headers=world["ha"])
    assert r.status_code == 500 and "hunter2" not in r.text and "/srv" not in r.text


# ── regression guards for the existing system ────────────────────────────────
def test_get_chat_llm_keeps_the_surface_the_agents_rely_on(world):
    llm = get_chat_llm()
    assert hasattr(llm, "invoke") and hasattr(llm, "with_structured_output")
    assert llm.invoke([HumanMessage(content="hi")]).content == f"answer from {BIG}"                 # no scope: legacy passthrough, unrecorded
    assert usage(world)["total"] == 0


def test_agents_construct_and_call_the_model_exactly_as_before(world, repository):
    from backend.agents.verification_agent import VerificationAgent

    agent = VerificationAgent()                                                                    # the production constructor path (get_chat_llm())
    assert agent.llm is get_chat_llm()
    with routing_scope(world["ua"], "verification"):
        out = invoke_with_retry(agent.llm, [HumanMessage(content="x")])
    assert out.content == f"answer from {BIG}" and usage(world)["items"][0]["purpose"] == "verification"


# ── the chat route is routed too ─────────────────────────────────────────────
def test_chat_calls_are_routed_recorded_as_chat_and_can_fall_back(world, monkeypatch):
    from types import SimpleNamespace

    import backend.api.routes.chat as chat_route

    class ChatClient(FakeClient):
        def with_structured_output(self, schema, **kw):
            outer = self

            class Structured:
                def invoke(self_inner, messages, *a, **kw2):
                    step = outer._next()
                    if isinstance(step, BaseException):
                        raise step
                    return SimpleNamespace(answer=f"chat answer from {outer.model}", suggested_questions=[])
            return Structured()

    world["router"]._client_factory = lambda m: ChatClient(m, world["scripts"].get(m, [ok()]), world["calls"])
    monkeypatch.setattr(chat_route, "_vector_memory", lambda: SimpleNamespace(collection=SimpleNamespace(count=lambda: 0)))
    monkeypatch.setattr(chat_route, "_llm", lambda: get_chat_llm().with_structured_output(dict))
    p = make_policy(world, model=MID, fallback_enabled=True, fallback_models=[SMALL])
    activate(world, p["policy_id"])
    ask = lambda who="a": world["c"].post("/api/chat/research", json={"topic": "Routing topic", "question": "How does routing work?"}, headers=world["h" + who])  # noqa: E731

    r = ask()
    assert r.status_code == 200 and r.json()["data"]["answer"] == f"chat answer from {MID}"
    script(world, MID, [Http(429)])
    r = ask()
    assert r.status_code == 200 and r.json()["data"]["answer"] == f"chat answer from {SMALL}"
    rows = usage(world)["items"]
    assert [x["purpose"] for x in rows] == ["chat", "chat"] and [x["fallback_used"] for x in rows] == [True, False]
    assert rows[0]["requested_model"] == MID and rows[0]["actual_model"] == SMALL and "rate limited (HTTP 429)" in rows[0]["fallback_reason"]
    assert all(x["tokens_reported"] is False and x["actual_cost_usd"] is None for x in rows)       # structured output carries no usage: unknown, not zero
    assert usage(world, "b")["total"] == 0 and current_scope() is None
