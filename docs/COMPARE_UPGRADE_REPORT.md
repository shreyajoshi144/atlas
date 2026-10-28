# Atlas Compare upgrade — implementation and verification report

Date of build: 2026-10-21. This report states what was built, what was actually tested, and what was **not**.
No accuracy figure is claimed anywhere: no model-quality measurement has been run, because that needs live
API keys and spend that were not available in the build sandbox.

## Headline limits (read first)

1. **No live provider call was ever made.** The sandbox cannot reach the provider APIs and had no keys. Request/response
   shapes were taken from each provider's official docs (2026-10-20) and tested against a local fake server that
   mimics them. First real use may reveal a field the docs summary got wrong; the code fails with a clear
   `parse`/`bad_request` error rather than guessing.
2. **The full application was not booted and the original Python test suite was not run.** PyPI is blocked in the sandbox
   (`fastapi`, `pytest`, `pydantic-settings`, `langchain`, `chromadb` unavailable). Route handlers were tested by calling
   them directly with a minimal FastAPI stub; FastAPI's own wiring (dependency injection, request parsing, response models)
   is unverified here. Run `python -m pytest -q` after `pip install -r requirements.txt` before relying on this build.
3. **Model IDs and prices come from provider docs read on 2026-10-20** (partly via page summaries) and move often.
   Each catalog entry has its source URL and verification date. Treat prices as estimates; verify IDs with
   `POST /api/compare/admin/discover` on your own keys.

## What was built

| Area | Implementation |
|---|---|
| Real providers | OpenAI and xAI (Responses API), Anthropic (Messages API), Google Gemini (generateContent); 11 catalog models. `backend/providers/adapters.py` |
| Server-side keys | Env/`.env` only; placeholders count as unconfigured; never serialized, logged or in URLs/bodies (tested) |
| Normalized responses | One `NormalizedResponse`: text, finish reason, usage (incl. reasoning/cached/search calls), citations, truncation, attempts |
| Timeouts / retries | Per-attempt timeout + overall deadline, exponential backoff with jitter, `Retry-After` honoured (capped), retries only for transient categories, every attempt shown to the user |
| Error handling | 18-category taxonomy; safe messages; no provider bodies or prompts leaked |
| Circuit breaker, concurrency | Per-provider breaker with single half-open probe; per-provider and global concurrency limits; redirects never followed; response-size and slow-drip protection |
| Tokens, cost, latency | Measured wall-clock latency per call; token usage as reported; cost from dated/tiered catalog prices (cache discount, search-tool fees, scheduled price changes); unknown is shown as unknown, never 0 |
| Compare | Same prompt to 1–4 models concurrently; partial failures keep other answers; measured differences only (fastest, cheapest…), no "best" wording |
| Sources & RAG | Atlas evidence mode (your library + Tavily web) gives every model identical labelled sources with sha256, retrieval time and injection risk flags; or each provider's own search tool, with provider-reported citations |
| Grounding checks | Citation validity, sentence coverage, lexical support — explicitly labelled a heuristic, not a factual judgement |
| Structured output | Optional JSON claims mode: native schema for OpenAI/xAI, prompted for Anthropic/Gemini, always validated locally, one repair attempt, failure reported not hidden |
| Routing / fallback | `/ask` with an explicit ordered chain; fallback only for provider trouble (never bad requests or safety refusals); fallback always disclosed. Compare never substitutes models |
| Guardrails / injection | Hidden-character stripping, credential detection (prompt rejected without echo; documents redacted before leaving), delimiter neutralization, untrusted-content wrapping, per-run canary for prompt leakage, markdown-image exfiltration removal, fabricated-citation detection |
| AuthN/Z | Existing bearer auth on all routes; runs are owner-scoped; admin routes require `role=admin` |
| Rate limits / concurrency | 6 runs per 5 minutes per user (in-process); one active run per user; idempotency keys replay without new calls |
| Budget | Reuses Atlas's per-user daily-call and monthly-spend reservations; per-request worst-case cost ceiling (default $0.50) refuses before any call; all-or-nothing reservation with rollback; cancellation keeps the reservation (conservative) |
| Observability | Structured logs (no content), in-process metrics (JSON + Prometheus text), persisted per-call metadata table, provider health/circuit state |
| Caching | Deliberately only model-discovery cache and idempotent replay. Response caching is off: it would corrupt latency/cost measurements |
| Privacy | Explicit consent flag, `store: false` where APIs offer it, disclosures shown in UI, owner-only saved runs, deletion, retention purge (default 90 days, run at service start) |
| Evaluation | 24-case dataset (grounded QA, multi-source, numeric, conflicting, unanswerable/abstain, injection, structured); deterministic scoring; Wilson 95% intervals; ranking claimed only if intervals separate; spend-capped CLI. **No results ship** |
| UI | New *Compare* area in the Meadow style (picker, cost gate, consent, side-by-side cards, grounding/cost/latency/reliability blocks, history, ask-with-fallback, beginner glossary). Playground and all existing views unchanged |

Existing files modified: `main.py` (router), `database/schema.sql` (3 additive tables), `.env.example`, `README.md`, `start.py` (hint text),
`frontend/app.html`, `frontend/js/script.js`, `frontend/css/atlas.css` (appended `cmp-*` rules, token colours only),
`frontend/tests/e2e/test_frontend_e2e.py` (nav count 6→7). Everything else is new (`backend/providers/`, `backend/api/routes/compare.py`,
`frontend/js/compare.js`, tests, `docs/COMPARE_API_CONTRACT.md`).

## Test results (build sandbox)

| Suite | Result |
|---|---|
| `python -m unittest discover -s backend/providers/tests -t .` | **121 passed, 0 failed** (units 41, HTTP client vs real local fake server 23, service + real SQLite budgets 36, evaluation 11, route handlers 10) |
| JS: compare, atlasbench, knowledge, playground, routing, workflow-studio | 16, 25, 34, 12, 14, 8 — **all pass** |
| `frontend/tests/e2e/test_compare_e2e.py` (real Chromium, mock API, 1280px and 360px) | **42/42 checks** |
| `frontend/tests/e2e/test_frontend_e2e.py` (original suite) | **111/113**. The 2 failures ("unchecked keep-signed-in uses session storage", "duplicate email error shown") also fail on the untouched original zip, so they are pre-existing; the original's third failing check did not reproduce here |
| `python -m compileall backend main.py start.py`; `node --check frontend/js/*.js` | pass |
| Original Python suite (`pytest backend/tests`) | **Not run** (dependencies unavailable) |
| Live provider calls, app boot, concurrency under real load, screen readers, other browsers | **Not run** |

What the tests prove: retry/backoff/`Retry-After`, timeout and slow-drip deadline, no redirect-following (key not forwarded), oversized
responses, breaker open/half-open/close, concurrency cap, all four request shapes and header placement, response normalization incl. citations,
truncation and refusals, budget ceilings and rollback against the real `OperationalRepository`, idempotency, per-user lock and cancellation,
injection/credential/canary/image-exfiltration handling, fallback rules, owner scoping, no key or prompt in any response or log, XSS-inert rendering.

## Security review notes

* Prompt-injection defences are layered mitigations, not a guarantee; the eval dataset includes injection cases so you can measure them on your models.
* Rate limits and the per-user run lock are per-process; behind multiple workers they are per-worker (the app's existing limiter has the same property).
* Provider-side retention is outside Atlas's control; `store:false` is requested where available.
* Timeouts after a provider has begun work may still be billed; failed attempts are not costed.

## Remaining blockers / next steps

1. `pip install -r requirements.txt`, then run the full original suite and the provider tests; fix anything the real dependency set reveals.
2. Add real keys, run `POST /api/compare/admin/discover`, then one small live Compare run per provider to confirm shapes. Points to watch:
   xAI `store` parameter acceptance, Gemini output-token limits per request, Anthropic web-search tool version (`web_search_20250305` used).
3. Run the evaluation (`--dry-run` first) on the models you care about; expect wide intervals with 24 cases — enlarge the dataset before drawing conclusions.
4. OpenAI web-search tool fees and some context windows are absent from the docs read, so those costs show as incomplete rather than guessed.
5. Streaming, semantic (LLM-judge) grounding, multi-process shared rate limits and distributed locks are not implemented.
