"""M8 — AtlasBench UI: wiring, safety patterns, Node-run behaviour tests, and a UI↔API contract
(every endpoint the UI calls exists; real API payloads render without ``undefined``/``NaN``)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.test_m8_api import BASE, _fresh_state, app, body, new_run, run_done  # noqa: F401  (fixtures + helpers)
from backend.tests.m7_fixtures import _fake_embeddings, engine, evolving_scenario, repository, solar_claim, svc, world  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
NODE = shutil.which("node")


def read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ── wiring ───────────────────────────────────────────────────────────────────
def test_atlasbench_is_wired_into_navigation_router_and_load_order():
    html, script = read("app.html"), read("js/script.js")
    assert html.count('data-nav="atlasbench"') == 2                                         # desktop + mobile nav
    assert html.index('src="js/script.js"') < html.index('src="js/atlasbench.js"')          # helpers it uses load first
    assert html.index('src="js/knowledge.js"') < html.index('src="js/atlasbench.js"')
    assert "atlasbench: viewAtlasBench" in script and "atlasbench: ['Atlas', 'AtlasBench']" in script


def test_existing_navigation_is_preserved():
    html, script = read("app.html"), read("js/script.js")
    for nav in ("home", "research", "analytics", "kb", "knowledge"):
        assert f'data-nav="{nav}"' in html
    for view in ("home: viewHome", "research: viewResearch", "history: viewHistory", "kb: viewKB", "case: viewCase", "settings: viewSettings", "knowledge: viewKnowledge"):
        assert view in script


def test_knowledge_map_offers_benchmarking_a_claim():
    km = read("js/knowledge.js")
    assert 'data-act="bench-claim"' in km and "window.AtlasBench.fromClaim" in km
    assert "/tasks/from-claim" in read("js/atlasbench.js")


# ── safety patterns ──────────────────────────────────────────────────────────
def test_atlasbench_ui_has_no_dangerous_patterns():
    js = read("js/atlasbench.js")
    for bad in ("eval(", "new Function", "document.write", "javascript:", "onclick=", "onerror=", "onload=", "insertAdjacentHTML", "innerHTML +=", "localStorage", "sessionStorage"):
        assert bad not in js, bad
    for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", js):
        assert 'rel="noopener noreferrer"' in tag, tag
    assert "api_key" not in js.lower() and "authorization" not in js.lower()      # the UI never handles credentials itself


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_atlasbench_javascript_is_syntactically_valid():
    for rel in ("js/atlasbench.js", "js/knowledge.js", "js/script.js"):
        done = subprocess.run([NODE, "--check", str(FRONTEND / rel)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("test_file", ["atlasbench_ui.test.js", "knowledge_ui.test.js"])
def test_ui_behaviour_tests_pass_under_node(test_file):
    done = subprocess.run([NODE, str(ROOT / "backend" / "tests" / "js" / test_file)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout and "FAILED" not in done.stdout


# ── UI → API contract ────────────────────────────────────────────────────────
def ui_calls():
    """[(METHOD, '/runs/{}/execute')] for every API call the UI makes."""
    js = read("js/atlasbench.js")
    out = set()
    for fn, method in (("get", "GET"), ("apiPost", "POST"), ("apiDelete", "DELETE")):
        for m in re.finditer(rf"\b{fn}\(((?:API \+ )?(?:'[^']*'|encodeURIComponent\([^()]*\)|\s|\+)+)", js):
            expr = m.group(1).replace("API +", "").strip()
            if not expr.startswith("'"):
                continue
            tmpl = re.sub(r"\s*\+\s*", "", re.sub(r"encodeURIComponent\([^()]*\)", "{}", expr)).replace("'", "")
            out.add((method, tmpl))
    return out


def test_every_endpoint_the_ui_calls_exists_with_that_method(app):
    paths = app["main"].app.openapi()["paths"]
    known = {(m.upper(), re.sub(r"\{[^}]+\}", "{}", p[len(BASE):])) for p, item in paths.items() if p.startswith(BASE) for m in item}
    calls = ui_calls()
    assert len(calls) >= 12, calls
    missing = sorted(c for c in calls if c not in known)
    assert not missing, missing


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_real_api_payloads_render_without_undefined_or_nan(app, tmp_path):
    c, h = app["c"], app["ha"]
    big, small = run_done(app), run_done(app, config="preset-small-model")
    pending = new_run(app)
    runs = body(c.get(f"{BASE}/runs", headers=h))["items"]
    detail = body(c.get(f"{BASE}/runs/{big['run_id']}", headers=h))["run"]
    cases = [body(c.get(f"{BASE}/runs/{big['run_id']}/cases/{x['task_id']}", headers=h))["case"] for x in detail["cases"]]
    cases += [body(c.get(f"{BASE}/runs/{small['run_id']}/cases/{x['task_id']}", headers=h))["case"] for x in body(c.get(f"{BASE}/runs/{small['run_id']}", headers=h))["run"]["cases"]]
    cmp_ = body(c.get(f"{BASE}/compare", params={"run_ids": f"{big['run_id']},{small['run_id']}"}, headers=h))
    tasks = [body(c.get(f"{BASE}/tasks/{t['task_id']}", headers=h))["task"] for t in body(c.get(f"{BASE}/tasks", headers=h))["items"]]
    cfgs = body(c.get(f"{BASE}/configs", headers=h))["items"]
    ov = body(c.get(f"{BASE}/overview", headers=h))
    task_summaries = body(c.get(f"{BASE}/tasks", headers=h))["items"]
    payload = {"runs": runs, "detail": detail, "pending": body(c.get(f"{BASE}/runs/{pending['run_id']}", headers=h))["run"], "cases": cases, "cmp": cmp_,
               "tasks": tasks, "cfgs": cfgs, "ov": ov, "summaries": task_summaries}
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(payload))
    script = tmp_path / "render.js"
    script.write_text(f"""
const A = require({json.dumps(str(FRONTEND / 'js' / 'atlasbench.js'))});
const P = JSON.parse(require('fs').readFileSync({json.dumps(str(f))}, 'utf8'));
const out = [];
P.runs.forEach(r => out.push(A.runRow(r)));
out.push(A.runDetailHtml(P.detail), A.runDetailHtml(P.pending));
P.cases.forEach(c => out.push(A.caseDetailHtml(c)));
out.push(A.compareHtml(P.cmp), A.configFormHtml(P.ov));
P.tasks.forEach(t => out.push(A.taskDetailHtml(t))); P.summaries.forEach(t => out.push(A.taskRow(t))); P.cfgs.forEach(c => out.push(A.configRow(c)));
const bad = out.map((h, i) => [i, h.match(/undefined|NaN|\\[object|null(?![a-z])/g)]).filter(x => x[1]);
if (bad.length) {{ console.error('LEAKED', JSON.stringify(bad.slice(0, 5)), out[bad[0][0]].slice(0, 600)); process.exit(1); }}
console.log('rendered', out.length, 'fragments');
""")
    done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "rendered" in done.stdout
