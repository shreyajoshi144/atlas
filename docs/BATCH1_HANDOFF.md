# Atlas Batch 1 handoff — Playground UI + Knowledge quality

## Root causes and changes

- `frontend/css/style.css`: Playground content panels used `--bg1` (the navy chrome color) while inheriting dark content text; `--bg0` was also referenced without a root definition. Added a defined white surface, corrected Playground foreground/background contrast, improved source/model card wrapping, mobile composer layout, focus indicators, and reduced-motion behavior.
- `frontend/js/playground.js`: per-model failures were all labelled `Unavailable`, obscuring provider failures and timeouts. The renderer now distinguishes complete, provider failed, timed out, limit reached, and unavailable states. Responses are rendered independently, so a failed card does not remove a successful model's answer.
- `backend/knowledge/extraction.py`: a standalone month such as “February” could be promoted as a proper-noun entity when it appeared mid-sentence. Excluded standalone calendar month names while preserving multi-word names (e.g. “February Capital”) and acronyms such as CAGR.
- `frontend/js/knowledge.js`: interactive knowledge rows already used button semantics and `tabindex`, but lacked Enter/Space activation. Added delegated keyboard activation.
- Added focused regression coverage in `backend/tests/test_batch1_regressions.py`, plus supplementary checks in the existing Knowledge and frontend test modules. Updated `README.md` with Batch 1 behavior and verification commands.

## Verification results

Passed:

- `python -m pytest -q backend/tests/test_batch1_regressions.py --tb=short` — **5 passed**.
- `node backend/tests/js/knowledge_ui.test.js` — **34 passed**.
- `node backend/tests/js/routing_ui.test.js` — **13 passed**.
- `node backend/tests/js/atlasbench_ui.test.js` — **25 passed**.
- `for file in frontend/js/*.js; do node --check "$file"; done` — **passed for all frontend JS files**.
- `python -m compileall -q backend main.py start.py backend/tests` — **passed**.
- `unzip -t Atlas_Batch1_Playground_Knowledge.zip` — **passed**, no archive errors.

Blocked / not verified:

- Full suite `python -m pytest -q --tb=short` — collection interrupted with **13 errors and 2 skips** because this runtime lacks declared project dependencies, including `langgraph`, `langchain_core`, and `tavily`. This is an environment blocker, not a passing full-suite result.
- The existing API/authorization/isolation integration tests could not be collected for the same dependency reason; their behavior was not re-certified in this run.
- `python start.py` stopped before launching because no `.env` exists. It requested `GROQ_API_KEY` and `TAVILY_API_KEY` configuration; no credentials were fabricated or inserted.
- Interactive browser verification (desktop/tablet/mobile screenshots, console/network inspection, actual contrast measurement, and live end-to-end model/provider checks) was **not performed** because no browser runtime was available and provider credentials were not configured.

## Remaining limitations

- The conservative extractor is heuristic, not a full NER system. It reduces the confirmed month-name noise without attempting unsupported semantic typing for generic concepts.
- API-level regression coverage, ownership/isolation re-verification, and actual browser-rendered QA remain blocked by the environment above. **This ZIP is not being represented as production-ready.**
