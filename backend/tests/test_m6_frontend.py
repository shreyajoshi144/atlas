"""M6 — Knowledge Map UI: wiring, safety patterns, Node-run behaviour tests, and a
UI↔API contract check (the fields the UI reads must exist in real API payloads)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.m6_fixtures import _fake_embeddings, repository, svc  # noqa: F401  (pytest fixtures)
from backend.tests.test_m6_api import BASE, api, data, world  # noqa: F401  (fixtures + helpers)

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
NODE = shutil.which("node")


def read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ── wiring ───────────────────────────────────────────────────────────────────
def test_knowledge_view_is_wired_into_navigation_and_router():
    html, script = read("app.html"), read("js/script.js")
    assert html.count('data-nav="knowledge"') == 1  # mobile drawer; on desktop Knowledge is a Research sub-tab
    assert "['knowledge', 'Knowledge map']" in script
    assert html.index('src="js/script.js"') < html.index('src="js/knowledge.js"')  # helpers it uses load first
    assert "knowledge: viewKnowledge" in script and "knowledge: ['Atlas', 'Knowledge']" in script


def test_existing_views_and_nav_are_preserved():
    html, script = read("app.html"), read("js/script.js")
    for nav in ("home", "research", "analytics", "kb"):
        assert f'data-nav="{nav}"' in html
    for view in ("home: viewHome", "research: viewResearch", "history: viewHistory", "kb: viewKB", "case: viewCase", "settings: viewSettings"):
        assert view in script


# ── safety patterns ──────────────────────────────────────────────────────────
def test_knowledge_ui_has_no_dangerous_patterns():
    js = read("js/knowledge.js")
    for bad in ("eval(", "new Function", "document.write", "javascript:", "onclick=", "onerror=", "onload=", "insertAdjacentHTML"):
        assert bad not in js, bad
    # every external link opens safely
    for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", js):
        assert 'rel="noopener noreferrer"' in tag, tag
    # the only place persisted data reaches innerHTML is through esc()/helpers, never raw .textContent-less concat of ids
    assert "innerHTML +=" not in js


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_knowledge_ui_javascript_is_syntactically_valid():
    for rel in ("js/knowledge.js", "js/script.js"):
        done = subprocess.run([NODE, "--check", str(FRONTEND / rel)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_knowledge_ui_behaviour_tests_pass_under_node():
    done = subprocess.run([NODE, str(Path(__file__).parent / "js" / "knowledge_ui.test.js")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout


# ── UI ↔ API contract ────────────────────────────────────────────────────────
def test_every_endpoint_the_ui_calls_exists(api):
    _, _, main_module = api
    paths = set(main_module.app.openapi()["paths"])
    js = read("js/knowledge.js")
    used = set(re.findall(r"get\('/(\w+)", js)) | set(re.findall(r"API \+ '/(\w+)", js))
    assert {"summary", "investigations", "entities", "claims", "evidence", "graph", "changes"} <= used
    # M7: challenge calls go to their own router (/api/challenges); everything else is the knowledge map
    other_routers = {"challenges": "/api/challenges"}
    for seg in used:
        prefix = other_routers.get(seg, f"{BASE}/{seg}")
        assert any(p.startswith(prefix) for p in paths), seg
    assert any(p.endswith("/rebuild") for p in paths)


def test_api_payloads_contain_every_field_the_ui_reads(world):
    c, h = world["client"], world["ha"]
    g = lambda path, **params: data(c.get(BASE + path, params=params, headers=h))

    s = g("/summary")
    assert {"counts", "investigations", "map_is_complete", "stale_claims", "conflicting_claims", "integrity"} <= set(s)
    assert {"entities", "claims", "evidence", "sources"} <= set(s["counts"])
    assert {"total", "complete", "partial", "failed", "not_recorded"} <= set(s["investigations"])
    assert {"ok", "problem_count"} <= set(s["integrity"])

    inv = g("/investigations")["items"][0]
    assert {"report_id", "topic", "created_at", "knowledge_state", "is_complete", "comparable", "claim_count", "source_count", "dropped", "error_type"} <= set(inv)
    assert {"sources", "evidence", "claims"} <= set(inv["dropped"])

    ent = g("/entities", limit=5)["items"][0]
    assert {"entity_id", "name", "claim_count", "source_count", "graph_state"} <= set(ent)
    claim_keys = {"claim_id", "text", "status", "confidence", "evidence_counts", "stale", "graph_state", "observed_at", "last_verified_at", "first_seen_at"}
    claims = g(f"/entities/{ent['entity_id']}")["claims"]
    assert claims and claim_keys <= set(claims[0]) and {"supporting", "conflicting", "mixed"} <= set(claims[0]["evidence_counts"])
    assert {"is_stale", "reasons"} <= set(claims[0]["stale"])

    detail = g(f"/claims/{claims[0]['claim_id']}")
    assert claim_keys | {"evidence", "history", "entities"} <= set(detail)
    assert {"supporting", "conflicting", "mixed"} == set(detail["evidence"])
    ev = detail["evidence"]["supporting"][0]
    assert {"evidence_id", "passage", "stance", "strength", "source", "graph_state"} <= set(ev)
    assert {"domain", "url", "freshness", "changed_since_verified", "publish_date", "last_seen_at", "title"} <= set(ev["source"])
    assert {"label", "age_days"} <= set(ev["source"]["freshness"])
    assert {"report_id", "topic", "observed_at", "status", "confidence", "graph_state"} <= set(detail["history"][0])
    assert {"entity_id", "name"} <= set(detail["entities"][0])

    evd = g(f"/evidence/{ev['evidence_id']}")
    assert {"source", "claims", "graph_state"} <= set(evd) and {"domain", "url", "freshness", "publish_date", "last_seen_at", "title"} <= set(evd["source"])

    graph = g("/graph", focus=detail["claim_id"], depth=2)
    assert {"nodes", "edges", "focus", "truncated"} <= set(graph)
    assert {"id", "type", "label", "graph_state"} <= set(graph["nodes"][0]) and {"source", "target", "type"} <= set(graph["edges"][0])

    ra = world["ra"]
    d = g("/changes", base=ra["r1"], target=ra["r2"])
    assert {"base", "target", "warnings", "summary", "claims", "evidence", "sources"} <= set(d)
    for key in ("new", "strengthened", "weakened", "contradicted", "unchanged", "not_reobserved", "stale"):
        assert key in d["summary"] and key in d["claims"]
    assert {"added", "removed", "stance_changed", "stale"} <= set(d["evidence"]) and "changed" in d["sources"]
    assert {"claim_id", "text", "base", "target"} <= set(d["claims"]["strengthened"][0])
    assert {"topic", "created_at"} <= set(d["base"])
