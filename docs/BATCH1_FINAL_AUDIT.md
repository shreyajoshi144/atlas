# Atlas Batch 1 — Final comparative audit

Date: 2026-10-14

## Decision

**Not fully verified; do not label production-ready.** The candidate is retained with its existing Batch 1 changes. No additional application code changes were justified by the evidence available in this environment. Batch 2 and unrelated AtlasBench work were not started.

## Baseline vs candidate

Compared `Atlas_Final_Runnable.zip` (baseline) with `Atlas_Batch1_Closed.zip` (candidate) by file inventory and SHA-256 content hashes.

Candidate-only additions:
- `atlas/BATCH1_CLOSURE.md`
- `atlas/BATCH1_HANDOFF.md`
- `atlas/backend/tests/test_batch1_regressions.py`

Candidate modified:
- `atlas/README.md`
- `atlas/backend/knowledge/extraction.py`
- `atlas/backend/tests/test_m5_knowledge.py`
- `atlas/backend/tests/test_m9_frontend.py`
- `atlas/frontend/css/style.css`
- `atlas/frontend/js/knowledge.js`
- `atlas/frontend/js/playground.js`

No baseline files were removed. The candidate changes are limited to Batch 1 documentation, extraction/UI changes and related tests. No dependency pins were changed.

## Dependency environment

- Runtime: Python 3.13.5, Node.js v22.16.0, npm 10.9.2; Python 3.11 is not installed. Python 3.13 meets the stated 3.11+ floor.
- Created/used an isolated virtual environment outside the ZIP.
- Command: `python -m pip install -r requirements.txt`
- Result: **blocked**, package-index DNS resolution failed (`Temporary failure in name resolution`). No dependency pins were modified.
- Missing modules confirmed in the available test runtime: `langchain_core`, `langgraph`, `chromadb`, `tavily`.

## Exact test results

Commands were run from `atlas/` unless stated otherwise.

### Focused Batch 1 regression tests (candidate)

`python -m pytest -q backend/tests/test_batch1_regressions.py --tb=short`

**5 passed, 0 failed, 0 skipped.**

### Existing JavaScript tests (candidate)

- `node backend/tests/js/atlasbench_ui.test.js` — **25 passed**
- `node backend/tests/js/knowledge_ui.test.js` — **34 passed**
- `node backend/tests/js/playground_ui.test.js` — **6 passed**
- `node backend/tests/js/routing_ui.test.js` — **13 passed**

Total: **78 passed** across the four existing JavaScript test files.

### Python compilation and JavaScript syntax

- `python -m compileall -q backend main.py start.py backend/tests` — passed.
- `for file in frontend/js/*.js; do node --check "$file"; done` — passed for all frontend JavaScript files.

### Full Python suite — baseline and candidate

Command: `python -m pytest -q --tb=short`

- Baseline: **13 collection errors, 2 skipped**, exit code 2.
- Candidate: **13 collection errors, 2 skipped**, exit code 2.
- Errors are collection-time missing-dependency failures, principally `langchain_core` and `langgraph`; the package installation attempt could not reach the package index. No test suite green result is claimed.

### Authorization, isolation and API security — baseline and candidate

Command: `python -m pytest -q backend/tests/test_authorization_isolation.py backend/tests/test_api_security.py backend/tests/test_auth_security.py --tb=short`

- Baseline: **27 passed, 15 failed, 36 errors**, exit code 1.
- Candidate: **27 passed, 15 failed, 36 errors**, exit code 1.
- The failures/errors reproduced identically in baseline and candidate and are import/setup failures due to missing `tavily`, `chromadb`, and `langchain_core`. These results do **not** establish that authorization/isolation behavior passes. Because these suites cannot collect/run fully without dependencies, a critical isolation defect cannot be ruled out by this run.

## UI/browser and live integration

- Existing JS tests and syntax checks passed, but these are not a substitute for a real browser session.
- Chromium is present, but a headless `--dump-dom` attempt against the local static frontend timed out after 15 seconds and produced no DOM output. Actual browser verification is **unverified**: desktop/tablet/mobile rendering, contrast measurements, overflow, console/network errors, model selection/comparison, citations, keyboard interaction, entity selection, and evidence/source relationship flows were not observed in a functioning browser.
- App startup and live provider/API checks were not completed. The environment has no valid provider credentials configured, and required runtime dependencies could not be installed.

## Root causes and changes retained

- Entity extraction now treats standalone month names as temporal context rather than named entities while allowing multi-token proper names that include a month; it remains heuristic and conservative and does not invent evidence or unsupported types.
- Playground CSS and frontend state/rendering changes improve contrast, source layout, model/result state presentation, and provider failure handling; keyboard interaction and knowledge UI changes are covered by existing/focused tests.
- Regression tests added: `backend/tests/test_batch1_regressions.py`; related knowledge/frontend tests were updated as listed above.

## Remaining blockers / release recommendation

1. Restore package-index access (or use an approved wheelhouse) and install the declared requirements in an isolated environment without changing pins.
2. Re-run the complete Python suite and all authorization/isolation tests; investigate any non-environment failures before release.
3. Configure valid credentials and perform authenticated API/provider end-to-end tests.
4. Complete real-browser verification at desktop, tablet and mobile widths, including keyboard and source/evidence interactions.

The candidate ZIP is structurally packaged, but Batch 1 is **not fully verified** and must not be described as production-ready on the basis of these results.
