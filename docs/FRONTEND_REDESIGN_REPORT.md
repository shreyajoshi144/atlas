# Atlas frontend redesign: report

## What was built
A from-scratch frontend (HTML, CSS and view markup) using the supplied meadow photograph.
Backend, API routes, authentication, database and script logic are untouched.

| Surface | What changed |
| --- | --- |
| Landing (`index.html`) | New page: meadow hero with a layered grass edge, "Research you can trace. AI you can measure." Sections: Product (interactive claim-to-source trace with drawn connector threads), How it works, Features (four jobs: research with traceable evidence, evaluate AI systems, build workflows, control model execution), Use cases, Security, FAQ, a Sign up / Sign in invitation, full footer. |
| Auth (`auth.html`) | New split layout: cropped meadow panel plus an opaque ivory form. Same element IDs, endpoints and flows as before (sign in, sign up, forgot, reset, notices, `next` routing). |
| Legal and contact | Rebuilt on the shared public chrome. |
| Workspace (`app.html`, `atlas.css`) | New shell: left rail over a heavily blurred, muted meadow image, one opaque ivory sheet per task. Overview, Research (writing-desk composer with mode tiles), Case (reading layout, figures strip), Cases, Library, Settings, Playground and Workflow Studio re-laid out. Knowledge map, AtlasBench and Runtime & Control keep their working markup and are restyled to the new system. |
| Palette | Ivory, paper, charcoal, mauve, dusty rose, straw, dry grass, stem. No blue, green, orange or purple hues; no `backdrop-filter`. |
| Type | Fraunces (display), Hanken Grotesk (interface), DM Mono (IDs only), loaded from Google Fonts with serif/sans fallbacks. |
| Assets | `meadow-hero.jpg`, `meadow-panel.jpg`, `meadow-soft.jpg` derived from `atlas_bg.jpg` (the original is 736x414, so the hero is an upscale), plus grass-edge SVGs. Old `bg.jpg` and `auth-bg.jpg` removed. |

Small JS edits (logic unchanged): the avatar now targets `#accountAvatar`; the case header, Library figures, Settings layout and some label casing changed in `script.js` and `playground.js`; the SVG stroke hexes were remapped to the new palette.

## Tests: what was run, and what was not
Run here, all passing on the final code:
- `frontend/tests/e2e/test_frontend_e2e.py`: **113/113** checks in real Chromium (Playwright) against `tests/e2e/mock_server.py`. Covers landing structure and the trace interaction; auth validation, error, success, forgot and reset; the session guard; navigation through all six areas and the Research sub-views; opening a case and its tabs; a research run (success and a forced 500); Workflow select; Settings; Ctrl+K search; Ask Atlas; sign-out; no horizontal overflow on 9 routes at 1440/1024/768/390 px; the mobile drawer; and per-page accessibility basics (lang, title, one h1, labelled fields, named controls).
- Node unit tests in `backend/tests/js/`: atlasbench 25, knowledge 34, playground 12, routing 14, workflow studio 8 assertions, all pass. `node --check` passes on every JS file.
- `backend/tests/test_archive09_palette.py`: updated to the new eight-colour palette; its four tests pass (run directly, because pytest is not installed in this sandbox).

Not run, and why:
- **The Python backend test suite** (`pytest backend/tests`). The sandbox has no network, so FastAPI and the other requirements could not be installed. Several of those tests (`test_m6/m8/m9_frontend.py`, `test_playground.py`, `test_batch1_regressions.py`, `test_research_depth.py`) assert wiring in `app.html`, `script.js` and `atlas.css`. I kept the pinned strings (nav `data-nav` counts, script load order, tab labels, `value="brief" data-k="8"`, `id="setTopK"...`, and the CSS rules those tests read) and checked them by inspection, but they have **not been executed**. Please run `pytest` locally.
- The previous jsdom smoke test (`frontend/tests/smoke.test.js`) was replaced by the Playwright suite, because jsdom could not be installed offline.
- Nothing was tested against the real backend, Groq or Tavily; the e2e uses canned JSON. Real research output, Playground answers, AtlasBench runs and routing data were not rendered. Knowledge map, AtlasBench and Runtime views were checked for load and layout only in their empty states.

## Known limits
- Screenshots here used fallback fonts (Google Fonts was unreachable), so final typography will differ slightly.
- The source photo is low resolution; the hero is an upscale and is soft by nature.
- Knowledge map, AtlasBench and Runtime & Control got the new shell and styling, not new per-screen layouts.
- Populated Knowledge, AtlasBench and Routing screens could not be visually checked without real data.
