"""M9 — Routing & cost UI: wiring, safety patterns, Node-run behaviour tests, and a UI↔API contract
(every endpoint the UI calls exists with that method; real API payloads render without undefined/NaN)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.test_m9_service_api import (  # noqa: F401  (pytest fixtures + helpers)
    BASE, BIG, MID, SMALL, _fake_embeddings, _fresh_state, activate, body, make_policy, repository, research, script, seed_benchmarks, usage, world,
)
from backend.tests.test_m9_router import Http

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
NODE = shutil.which("node")


def read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_routing_ui_is_wired_into_atlasbench_and_loads_after_its_dependencies():
    html, ab = read("app.html"), read("js/atlasbench.js")
    assert html.index('src="js/atlasbench.js"') < html.index('src="js/routing.js"') and html.index('src="js/script.js"') < html.index('src="js/routing.js"')
    assert "['routing', 'Routing & cost']" in ab and "routing: drawRouting" in ab and "window.AtlasRouting.mount" in ab


def test_existing_atlasbench_and_knowledge_tabs_are_preserved():
    ab = read("js/atlasbench.js")
    for tab in ("['runs', 'Runs']", "['compare', 'Compare']", "['tasks', 'Tasks']", "['configs', 'Configurations']"):
        assert tab in ab
    assert 'data-nav="atlasbench"' in read("app.html") and 'data-nav="knowledge"' in read("app.html")


def test_routing_ui_has_no_dangerous_patterns():
    js = read("js/routing.js")
    for bad in ("eval(", "new Function", "document.write", "javascript:", "onclick=", "onerror=", "onload=", "insertAdjacentHTML", "innerHTML +=", "localStorage", "sessionStorage"):
        assert bad not in js, bad
    assert "api_key" not in js.lower() and "authorization" not in js.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_routing_javascript_is_syntactically_valid():
    for rel in ("js/routing.js", "js/atlasbench.js"):
        done = subprocess.run([NODE, "--check", str(FRONTEND / rel)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("test_file", ["routing_ui.test.js", "atlasbench_ui.test.js", "knowledge_ui.test.js"])
def test_ui_behaviour_tests_pass_under_node(test_file):
    done = subprocess.run([NODE, str(ROOT / "backend" / "tests" / "js" / test_file)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout and "FAILED" not in done.stdout


def ui_calls():
    js = read("js/routing.js")
    out = set()
    for fn, method in (("get", "GET"), ("apiPost", "POST"), ("apiPut", "PUT"), ("apiDelete", "DELETE")):
        for m in re.finditer(rf"\b{fn}\(((?:API \+ )?(?:'[^']*'|encodeURIComponent\([^()]*\)|\s|\+)+)", js):
            expr = m.group(1).replace("API +", "").strip()
            if not expr.startswith("'"):
                continue
            out.add((method, re.sub(r"\s*\+\s*", "", re.sub(r"encodeURIComponent\([^()]*\)", "{}", expr)).replace("'", "")))
    return out


def test_every_endpoint_the_routing_ui_calls_exists_with_that_method(world):
    paths = world["main"].app.openapi()["paths"]
    known = {(m.upper(), re.sub(r"\{[^}]+\}", "{}", p[len(BASE):])) for p, item in paths.items() if p.startswith(BASE) for m in item}
    calls = ui_calls()
    assert len(calls) >= 8, calls
    assert not sorted(c for c in calls if c not in known), sorted(c for c in calls if c not in known)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_real_api_payloads_render_without_undefined_or_nan(world, research, tmp_path):
    c, h = world["c"], world["ha"]
    seed_benchmarks(world)
    p_auto = make_policy(world, mode="autopilot", model=BIG, min_quality=0.9, fallback_enabled=True, fallback_models=[BIG, SMALL])
    p_exp = make_policy(world, model=SMALL)
    activate(world, p_auto["policy_id"])
    research()                                                           # routed calls on the autopilot's pick
    script(world, MID, [Http(503)])
    research()                                                           # a fallback
    script(world, MID, [Http(400, "bad request")])
    research()                                                           # a failure that is NOT a fallback case
    previews = [body(c.post(f"{BASE}/preview", json={"policy_id": p["policy_id"], "prompt_tokens": 4000}, headers=h)) for p in (p_auto, p_exp)]
    previews.append(body(c.post(f"{BASE}/preview", json={"prompt_tokens": 10}, headers=world["hb"])))
    payload = {"overview": body(c.get(f"{BASE}/overview", headers=h)), "policies": body(c.get(f"{BASE}/policies", headers=h)), "usage": usage(world),
               "fb": usage(world, fallback_only="true"), "summary": body(c.get(f"{BASE}/usage/summary", headers=h)), "previews": previews,
               "empty_usage": usage(world, "b"), "empty_summary": body(c.get(f"{BASE}/usage/summary", headers=world["hb"])),
               "bob_overview": body(c.get(f"{BASE}/overview", headers=world["hb"]))}
    assert payload["usage"]["total"] == 6 and any(r["fallback_used"] for r in payload["usage"]["items"]) and any(r["status"] == "failed" for r in payload["usage"]["items"])
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(payload))
    script_js = tmp_path / "render.js"
    script_js.write_text(f"""
const R = require({json.dumps(str(FRONTEND / 'js' / 'routing.js'))});
const P = JSON.parse(require('fs').readFileSync({json.dumps(str(f))}, 'utf8'));
const out = [R.usageTable(P.usage.items), R.usageTable(P.fb.items), R.usageTable(P.empty_usage.items), R.summaryHtml(P.summary), R.summaryHtml(P.empty_summary),
  R.activeBanner(P.overview), R.activeBanner(P.bob_overview), R.policyFormHtml(P.overview)];
P.policies.items.forEach(p => out.push(R.policyRow(p)));
P.previews.forEach(p => out.push(R.decisionHtml(p)));
const bad = out.map((h, i) => [i, h.match(/undefined|NaN|\\[object|null(?![a-z])/g)]).filter(x => x[1]);
if (bad.length) {{ console.error('LEAKED', JSON.stringify(bad.slice(0, 5)), out[bad[0][0]].slice(0, 700)); process.exit(1); }}
console.log('rendered', out.length, 'fragments');
""")
    done = subprocess.run([NODE, str(script_js)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "rendered" in done.stdout


def test_playground_batch1_has_light_surface_accessible_controls_and_responsive_layout():
    css = read("css/atlas.css")
    js = read("js/playground.js")
    assert ".pg-history, .pg-workspace { background: var(--ivory); color: var(--ink);" in css
    assert ".pg-composer-footer { align-items: stretch; flex-direction: column; }" in css
    assert "focus-visible" in css and "prefers-reduced-motion" in css
    assert "data-pg-model" in js and "Run comparison" in js
    assert "role=\"status\"" in js and "rel=\"noopener noreferrer\"" in js


def test_knowledge_rows_support_keyboard_activation():
    js = read("js/knowledge.js")
    assert "root().addEventListener('keydown', onKeyboardActivate)" in js
    assert "function onKeyboardActivate(ev)" in js
    assert "ev.key !== 'Enter' && ev.key !== ' '" in js


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_playground_render_keeps_success_when_another_model_fails():
    js = read("js/playground.js")
    # Exercise the real output renderer in Node without a browser or fake provider response.
    runner = f"""
global.window = {{}}; global.escapeHtml = s => String(s); global.mdToHtml = s => String(s);
require({json.dumps(str(FRONTEND / 'js' / 'playground.js'))}); const P = window.AtlasPlayground;
const html = P.outputHtml({{model:'model-a', label:'Model A', status:'ok', answer:'Real answer', usage:{{}}}}) +
  P.outputHtml({{model:'model-b', label:'Model B', status:'failed', error:'Provider unavailable', usage:{{}}}});
if (!html.includes('Real answer') || !html.includes('Provider failed') || !html.includes('Provider unavailable')) process.exit(1);
if (!html.includes('Complete')) process.exit(1);
console.log('playground partial-success render passed');
"""
    done = subprocess.run([NODE, "-e", runner], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout
