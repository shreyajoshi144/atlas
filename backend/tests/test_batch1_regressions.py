"""Batch 1 regressions that intentionally do not require optional LLM packages."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_standalone_month_is_not_promoted_but_acronym_and_named_org_survive():
    from backend.knowledge.extraction import HeuristicEntityExtractor

    extracted = HeuristicEntityExtractor().extract(
        "Revenue increased in February. CAGR remained a useful metric for Acme Energy."
    )
    assert "february" not in {name.casefold() for name, _ in extracted}
    assert ("CAGR", "acronym") in extracted
    assert ("Acme Energy", "proper_noun") in extracted


def test_month_in_a_multiword_name_is_preserved():
    from backend.knowledge.extraction import HeuristicEntityExtractor
    assert ("February Capital", "proper_noun") in HeuristicEntityExtractor().extract(
        "February Capital announced a new fund."
    )


def test_playground_light_surface_focus_and_mobile_rules_are_present():
    css = (ROOT / "frontend/css/atlas.css").read_text()
    assert ".pg-history, .pg-workspace { background: var(--ivory); color: var(--ink);" in css
    assert "focus-visible" in css and "prefers-reduced-motion" in css
    assert "@media (max-width: 620px)" in css
    assert ".pg-composer-footer { align-items: stretch; flex-direction: column; }" in css


def test_knowledge_rows_support_keyboard_activation():
    js = (ROOT / "frontend/js/knowledge.js").read_text()
    assert "root().addEventListener('keydown', onKeyboardActivate)" in js
    assert "function onKeyboardActivate(ev)" in js


def test_playground_partial_success_output_renderer_under_node():
    node = shutil.which("node")
    if not node:
        return
    path = str(ROOT / "frontend/js/playground.js").replace("\\", "\\\\").replace("'", "\\'")
    script = f"""global.window = {{}}; global.escapeHtml = s => String(s); global.mdToHtml = s => String(s);
require('{path}'); const P = window.AtlasPlayground;
const html = P.outputHtml({{model:'model-a',label:'Model A',status:'ok',answer:'Real answer',usage:{{}}}}) +
 P.outputHtml({{model:'model-b',label:'Model B',status:'failed',error:'Provider unavailable',usage:{{}}}});
if (!html.includes('Real answer') || !html.includes('Provider failed') || !html.includes('Provider unavailable') || !html.includes('Complete')) process.exit(1);
console.log('partial-success renderer passed');"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout
