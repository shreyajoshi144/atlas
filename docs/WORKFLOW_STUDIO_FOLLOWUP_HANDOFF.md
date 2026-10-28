# Workflow Studio Follow-up — Management, Snapshots, Interrupted Runs

## Scope and change control

Based on `Atlas_AtlasBench_WorkflowStudio.zip`, itself based on `Atlas_Batch1_CostLabel_Fix.zip`. Work was performed in a separate directory; the source ZIP was not modified. Batch 1 Playground, Knowledge, and cost-label work is retained. No Batch 2 enhancements or unrelated AtlasBench code changes were made.

## Confirmed findings and root causes

1. **Saved workflow management actions were missing.** `frontend/js/workflow-studio.js` rendered select/create/execute/history only. Added edit using the existing PUT endpoint, plus new duplicate and delete endpoints. Every service operation uses the authenticated user ID; foreign-owned resources return not found. Deleting a workflow deletes its run history via existing foreign-key cascade behavior.
2. **Run history did not preserve configuration.** Run rows stored workflow ID and input only; step rows stored type/name/input/output/error but omitted step parameters. Added a versioned `definition_snapshot_json`/`snapshot_version` to each run and `config_json` to each step. New schema and in-place migrations are both covered. A regression test edits a workflow after execution and proves the original `append_text` parameter remains recoverable.
3. **Interrupted runs could remain `running` indefinitely.** Execution commits `running` before executing steps. Added a conservative terminal-failure policy: when a run is observed in `running` state after the 120-second recovery window, it becomes `failed` with an explicit interruption error. It is not automatically replayed, preventing implicit repetition if future steps acquire side effects. The same idempotency key returns the stored failed record; callers are directed to use a new key to explicitly resubmit.
4. **AtlasBench M8 remains unverified.** No AtlasBench code was changed because the existing M8 service/runner tests could not be collected. Both fail at import with `ModuleNotFoundError: langchain_core`. An isolated `pip install -r requirements.txt` was attempted; DNS resolution for the package index failed. Controlled-provider full-lifecycle verification is still blocked, and this package does not claim AtlasBench recertification.

## Files changed

- `backend/workflows/service.py` — migration, run snapshots, per-step config persistence, duplicate/delete service methods, interrupted-run terminalization.
- `backend/api/routes/workflows.py` — duplicate and delete endpoints using existing auth dependency and ownership-scoped service calls.
- `backend/database/schema.sql` — columns for fresh databases.
- `frontend/js/workflow-studio.js` — Edit, Duplicate and Delete actions.
- `backend/tests/test_workflow_studio.py` — immutable run snapshot, duplicate/delete ownership, interrupted-run terminalization, legacy-schema migration tests.
- `backend/tests/js/workflow_studio_ui.test.js` — assertions for rendered management controls.
- `WORKFLOW_STUDIO_FOLLOWUP_HANDOFF.md` — this report.

## Exact verification results

- `python -m pytest -q backend/tests/test_workflow_studio.py backend/tests/test_batch1_regressions.py` — **14 passed, 0 failed**.
- `node --test backend/tests/js/*.test.js` — **5 test files passed, 0 failed, 0 skipped**; suites report **92 checks/assertions** (AtlasBench UI 25, Knowledge UI 34, Playground UI 12, Routing UI 13, Workflow Studio UI 8).
- `python -m compileall -q backend main.py` — **passed**.
- `for f in frontend/js/*.js; do node --check "$f" || exit 1; done` — **passed** for all frontend JavaScript files.
- `python -m pytest -q backend/tests/test_m8_service.py backend/tests/test_m8_runner.py` — **blocked at collection**, 2 errors due to missing `langchain_core`; no M8 tests executed.
- Isolated environment install: `python -m venv /mnt/data/atlas_m8_venv && /mnt/data/atlas_m8_venv/bin/python -m pip install -r requirements.txt` — **blocked** by DNS failure resolving package index; no dependency pins were changed.
- Real browser and live provider checks — **not performed**; browser and credential verification remains outstanding.

## Remaining limitations

The 120-second interruption rule terminalizes only when a running run is subsequently read (including history reads). It fails stale runs rather than restarting them. A workflow run's historical configuration is available in the run response as `definition_snapshot`, with each step's normalized configuration under `steps[].config`. AtlasBench M8 and browser-level end-to-end checks remain blocked/unverified.
