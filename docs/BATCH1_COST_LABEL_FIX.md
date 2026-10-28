# Atlas Batch 1 — Playground Cost Label Fix

## Root cause and contract semantics

The Playground footer used the ambiguous labels “List estimate” and “Usage-based estimate”, and missing costs rendered as `unknown`. Inspection of `backend/api/routes/playground.py`, `backend/routing/router.py`, `backend/routing/selector.py`, and `backend/ops/README.md` establishes:

- `usage.estimated_cost_usd` is sourced from the routing decision's `est_cost_usd`. For the explicit model path, `est_call_cost()` estimates cost before the call using an estimated prompt-token count, a benchmark-derived average completion-token count (or default completion allowance), and the model catalog's token prices. It is **not** a guaranteed provider list-price charge. The UI now calls it **Pre-call cost estimate**.
- `usage.actual_cost_usd` is calculated by Atlas from provider-reported prompt/completion token counts and Atlas's model catalog prices. Providers do not report USD charges to Atlas; this is **Usage-based cost estimate**, not an invoice or provider-billed actual.
- Missing or invalid values display **Not reported**. Numeric zero remains a valid `$0.0000` and zero tokens/latency remain valid values. Non-finite, malformed and negative values are not formatted as amounts.

No backend calculation or response contract was changed.

## Files changed

- `frontend/js/playground.js` — accurate footer labels; finite/nonnegative number validation; missing-data handling; safe token and latency rendering; updated explanatory copy.
- `backend/tests/js/playground_ui.test.js` — regression tests against the actual `outputHtml()` / `messageHtml()` rendered HTML for populated costs, one missing estimate, missing usage, zero values, malformed/non-finite values, and partial success with provider failure. Existing tests retained.
- `BATCH1_COST_LABEL_FIX.md` — this handoff.

## Exact verification

Commands run from `atlas/`:

- `node backend/tests/js/playground_ui.test.js` — **12 passed**
- `node backend/tests/js/knowledge_ui.test.js` — **34 passed**
- `node backend/tests/js/routing_ui.test.js` — **13 passed**
- `node backend/tests/js/atlasbench_ui.test.js` — **25 passed**
- `for f in frontend/js/*.js; do node --check "$f" || exit 1; done` — **passed**
- `python -m pytest -q backend/tests/test_batch1_regressions.py` — **5 passed**

Total existing JavaScript test scripts run: **84 passed, 0 failed**. Targeted Python regression suite: **5 passed, 0 failed**. No browser/live-provider test was needed for this presentation-only fix; these results do not claim a live browser run.
