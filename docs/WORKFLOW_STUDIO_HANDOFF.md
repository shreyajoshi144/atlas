# Atlas workflow / AtlasBench completion handoff

## Scope and baseline

Built on `Atlas_Batch1_CostLabel_Fix.zip` in a separate working directory. The supplied ZIP was not edited. Existing Batch 1 Playground, Knowledge, and cost-label changes are retained. No dependency pins were changed and no unrelated AtlasBench features were added.

## AtlasBench

Inspected the existing M8 implementation in `backend/bench/{models,repository,runner,service}.py`, `backend/api/routes/atlasbench.py`, `frontend/js/atlasbench.js`, and M8 tests. The existing implementation already persists task snapshots and per-case results, uses conditional state updates and run leases, bounds retries, returns stored completed results on repeated execute requests, records partial summaries, and scopes resource reads/writes to the authenticated user. No AtlasBench code was changed because no new root-cause defect was established by the available verification. Existing AtlasBench integration tests could not be collected in this environment (see blockers).

## Workflow Studio implementation

Added a minimal deterministic vertical slice. Supported step types are `input_text`, `normalize_whitespace`, `append_text`, `uppercase`, and `require_contains`. Unsupported types are rejected by server-side validation. Workflows and runs are separate persisted records; run step outcomes and run history are persisted. All resource queries are scoped by authenticated user. Execution input/output is bounded to 10,000 characters, definitions to 10 steps, and idempotency keys are unique per user/workflow. Reusing a key with the same input returns the stored run; using it with a different input returns a conflict. Steps have no external side effects, so retries do not duplicate external actions. No retries or cancellation are advertised.

## Changed files

- `backend/workflows/__init__.py`
- `backend/workflows/service.py`
- `backend/api/routes/workflows.py`
- `backend/database/schema.sql`
- `main.py` (route registration)
- `frontend/js/workflow-studio.js`
- `frontend/js/script.js` (view registration and custom request headers)
- `frontend/index.html` (desktop/mobile navigation and script inclusion)
- `backend/tests/test_workflow_studio.py`
- `backend/tests/js/workflow_studio_ui.test.js`
- `WORKFLOW_STUDIO_HANDOFF.md`

## Verification performed

- `python -m compileall -q backend main.py` — passed.
- Full `SQLiteManager.initialize_database()` against an isolated temporary database with test-only required environment keys — passed; all three workflow tables were created.
- `node --check` for every `frontend/js/*.js` — passed.
- `node --test backend/tests/js/*.test.js` — 5 test files passed, 0 failed; 89 assertions/checks reported by suites (AtlasBench 25, Knowledge 34, Playground 12, Routing 13, Workflow Studio renderer 5).
- `python -m pytest -q backend/tests/test_workflow_studio.py` — 5 passed, 0 failed.
- `python -m pytest -q backend/tests/test_batch1_regressions.py` — 5 passed, 0 failed.
- Combined targeted Python total: 10 passed, 0 failed.

## Limitations / not verified

- `backend/tests/test_m8_service.py` and `backend/tests/test_m8_runner.py` could not be collected because `langchain_core` is not installed (`ModuleNotFoundError`). The broader AtlasBench and full Python suite were not run successfully in this environment.
- No live model provider calls were made.
- No real browser session was available/performed for the new Workflow Studio UI. The renderer was smoke-tested under Node; responsive browser layout and real authenticated end-to-end API operation remain unverified.
- Workflow Studio intentionally supports deterministic text operations only; it does not expose arbitrary code, network calls, LLM calls, external side effects, retry policies, or cancellation.

This is not a production-readiness certification.
