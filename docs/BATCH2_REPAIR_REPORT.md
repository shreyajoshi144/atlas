# Atlas Batch 2 Repair — Verification Report

## Baseline and change control

- Working baseline: `Atlas_AtlasBench_WorkflowStudio_Followup.zip` (extracted to a separate working directory).
- The source ZIP was not modified. Existing Batch 1 cost-label, Knowledge, Workflow Studio, ownership, run-snapshot, and interrupted-run changes were retained.
- No dependency pins, database schema, API request/response schemas, benchmark inputs, evaluator semantics, or frontend product scope were changed.

## Root causes and repairs

1. **Workflow Studio routes were imported but not registered.** `main.py` imported `workflows_router` but never included it in the FastAPI application. Registered it at `/api/workflows` with the same `_auth_dep` app-level authentication protection used for other user-scoped APIs.
2. **AtlasBench could leave an exhausted crashed run in `running` forever.** `BenchRepository.begin_run()` could reclaim stale runs only while `attempt_count < MAX_RUN_ATTEMPTS`; a crash on the last attempt left a stale `running` row that could never be claimed again. Added a terminal reconciliation policy: expired `running` rows at the attempt cap become `failed` with `interrupted_max_attempts_exhausted`. Reconciliation occurs when a run is read/listed and before a new execution claim. Stale runs with attempts remaining remain eligible for the existing resume path. No execution is replayed by reconciliation.
3. **Regression coverage added.** Added an actual-app route/auth test and an authenticated Workflow Studio API lifecycle test (create/list/read/execute/history/run read, duplicate idempotency, invalid definitions, failed-step persistence, and cross-user denial). Added an AtlasBench service regression for an exhausted stale run and a dependency-light persistence test for the terminalization policy.

## Files changed

- `atlas/main.py`
- `atlas/backend/bench/repository.py`
- `atlas/backend/bench/service.py`
- `atlas/backend/tests/test_m8_service.py`
- `atlas/backend/tests/test_workflow_studio_app_routes.py` (new)
- `atlas/backend/tests/test_m8_recovery_policy.py` (new)
- `atlas/BATCH2_REPAIR_REPORT.md` (this report)

## Exact verification results in this environment

- `pytest -q backend/tests/test_workflow_studio.py backend/tests/test_batch1_regressions.py backend/tests/test_m8_recovery_policy.py`: **15 passed, 0 failed**.
- `node --test backend/tests/js/*.test.js`: **5 test files passed, 0 failed; 92 assertions/checks reported by the suites** (AtlasBench 25, Knowledge 34, Playground 12, Routing 13, Workflow Studio 8).
- `node --check frontend/js/workflow-studio.js`: **passed**.
- `python -m compileall -q main.py backend`: **passed**.
- Full `pytest -q`: **collection blocked; 13 collection errors, 2 skipped, 0 test failures reported because collection did not complete**. Errors are caused by missing declared packages: `langchain_core`, `langgraph`, and `tavily`.
- `pytest -q backend/tests/test_workflow_studio_app_routes.py`: **2 failed before assertions** because importing `main.py` imports `backend.api.routes.chat`, which requires missing `langchain_core`. These are environment/import failures, not passing API tests.
- Focused AtlasBench M8 service/runner/API tests could not be collected because test fixtures require missing `langchain_core`. The API tests and M8 recovery regression are included but not represented as passing.
- Isolated environment dependency installation was attempted with `python -m venv ...` followed by `pip install -r requirements.txt`; it failed because package-index DNS resolution was unavailable (`Temporary failure in name resolution`). Dependency declarations were not changed.
- Browser automation: Chromium executable exists, but neither Playwright nor Puppeteer is installed, and the backend cannot import/start without the declared dependencies. Real-browser, live-provider, and full authenticated end-to-end checks remain **unverified**.

## Setup and verification commands

From the `atlas/` project directory, with Python 3.11+ and package-index access:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
pytest -q
node --test backend/tests/js/*.test.js
node --check frontend/js/workflow-studio.js
python -m compileall -q main.py backend
```

Configure the required provider/API environment variables using `.env.example` before attempting live-provider flows. Do not commit real credentials. Start with the documented project command (`python start.py`) after dependencies and environment configuration are present.

## Release status

The confirmed router-registration defect is fixed in source and covered by a runtime test that is currently blocked by missing dependencies. The exhausted AtlasBench stale-run terminalization policy and focused regression test pass. Full Batch 2 verification is **incomplete** until dependencies can be installed and the app-level, M8, security, and browser tests are run in a configured environment. No production-readiness claim is made.
