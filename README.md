# Atlas AI

Atlas is a FastAPI research application with a static frontend. This repository
contains the M1–M9 research, retrieval, Knowledge Map, Challenge, AtlasBench,
and routing features, plus M10 benchmark-output arbitration, M11 operations,
and the AI Playground for explicit multi-model comparisons.
AtlasBench arbitration compares only completed outputs scored against the same
frozen benchmark task and evidence. It does not score arbitrary Playground
questions against benchmark gold data or call an LLM judge.

The authenticated Playground can compare up to three manually selected,
allowlisted Groq models against the same question and retrieved evidence. It
can use the signed-in user's indexed research library and optional Tavily web
search, shows source IDs and provider-derived usage estimates, and stores
conversation history per account. Explicit Playground selections apply only to
that comparison; saved M9 routing policy remains in effect elsewhere, and
fallback is disabled for these side-by-side calls.

## Compare: OpenAI, Anthropic, Google Gemini and xAI Grok (new)

The **Compare** area (top bar → *Compare*, API under `/api/compare`) sends one question to up to four
models from four providers using their real APIs, and shows each answer with its sources, citations,
token usage, estimated cost, measured latency and any failures/retries. It never ranks models on
opinion: quality can only be ranked from measured evaluation results (see *Evaluation* below), and the UI says
plainly when none exist. The existing Playground, AtlasBench, routing, Workflow Studio and Meadow UI are unchanged.

**Setup.** Add the keys you have to `.env` (a provider with no key is simply shown as not configured):

```
OPENAI_API_KEY=...      # also used for embeddings; "dummy"/placeholder values count as not configured
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...      # GOOGLE_API_KEY also works
XAI_API_KEY=...
```

`GROQ_API_KEY` and `TAVILY_API_KEY` are still required by the original research pipeline (use placeholders
if you only want Compare; research runs will then fail at call time). Keys stay on the server, are never
returned by any endpoint and are scrubbed from logs. Then `pip install -r requirements.txt` and `python start.py`.
Optional tuning variables are listed in `.env.example` (per-request cost ceiling, timeouts, retries, retention).

**Model IDs and prices change often.** `backend/providers/catalog.json` holds the IDs and list prices, each with a
source URL and the date they were verified (2026-10-20). The UI shows a staleness warning after 60 days. As an
admin (`users.role='admin'`, see `backend/ops/README.md`) call `POST /api/compare/admin/discover` to check the
catalog against what your keys can actually use; models a key cannot use are blocked with a clear message.
Costs are estimates from catalog prices (or provider-reported cost where given), never invoices.

**Evaluation (the only basis for "which is better").** `python -m backend.providers.eval_cli --models
openai:gpt-6.1-sol,anthropic:claude-sonnet-5-5 --max-usd 2` runs a 24-case grounding/abstention/injection/structured-output
dataset against the real APIs (add `--dry-run` to see the worst-case cost first) and stores results that the
Compare page then shows with confidence intervals. Nothing ships pre-computed. Small samples usually cannot
separate models; the page only claims a ranking when intervals do not overlap.

**Tests.** `python -m unittest discover -s backend/providers/tests -t .` (121 tests; runs on the standard library
against a local fake provider server and a real SQLite file), `node backend/tests/js/compare_ui.test.js`, and
`python frontend/tests/e2e/test_compare_e2e.py` (browser, mock API). See `COMPARE_UPGRADE_REPORT.md` for results and limits.

## Requirements

- Python 3.11 or newer
- Node.js for JavaScript syntax checks (optional for serving the app)
- Groq and Tavily API credentials for live model and web-search operations
- No Docker is required

## Local setup

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and replace the `GROQ_API_KEY`, `TAVILY_API_KEY`, and
`SECRET_KEY` placeholders. The example values are not credentials. The default
embedding model is local; `OPENAI_API_KEY` is optional. Start the app with:

```sh
python start.py
```

`start.py` launches two local processes: the FastAPI backend on
`http://127.0.0.1:8000` and Python's static file server on
`http://127.0.0.1:5500`. Open the landing page at
`http://127.0.0.1:5500/index.html`; sign in at `auth.html`; the workspace is
`http://127.0.0.1:5500/app.html`. On localhost the frontend defaults to the
backend at port 8000. For a separate deployed frontend/backend, set
`window.ATLAS_API_BASE` in `frontend/js/config.js` to the API origin (with or
without `/api`) and configure the backend's CORS origins accordingly.
`/health` and `/ready` are backend endpoints on port 8000.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` | Yes | Primary LLM provider |
| `TAVILY_API_KEY` | Yes | Web search provider |
| `SECRET_KEY` | Production | Random signing secret, at least 32 characters |
| `APP_ENV` | No | `development`, `staging`, or `production` |
| `CORS_ALLOWED_ORIGINS` | Production | Comma-separated exact deployed frontend origins |
| `FRONTEND_BASE_URL` | No | Frontend URL used in account flows |
| `OPENAI_API_KEY` | No | Optional embedding integration |
| `DEFAULT_DAILY_LLM_CALL_LIMIT` | No | Default is 1000 budget events per user per UTC day; a routed fallback chain counts once, each selected Playground model counts once, and AtlasBench counts each guarded LLM invocation |
| `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` | Production | Nonnegative USD default monthly cap; optional in development, required at production startup |

Storage paths, retrieval tuning, provider timeouts, and other supported
settings are defined in `backend/core/config.py`. Unexpected server errors use
a generic client response in every environment; structured logs retain exception
type and sanitized stack locations but omit raw exception messages and request
query strings, which can contain credentials or user content. Never commit `.env`
or real provider credentials. In production set a unique random `SECRET_KEY`,
`APP_ENV=production`, explicit non-wildcard `CORS_ALLOWED_ORIGINS`, and
`DEFAULT_MONTHLY_LLM_SPEND_CAP_USD`; startup fails if the cap is omitted. The
cap uses token-based price estimates and is not a guaranteed hard spend ceiling.
Keep secrets in the deployment platform's secret store. Promote administrator accounts through a
controlled database administration process; see [backend/ops/README.md](backend/ops/README.md).

## Database and migrations

Atlas uses SQLite. The backend's startup lifespan calls
`SQLiteManager.initialize_database()`, which creates
the schema and applies additive compatibility migrations for existing
databases. The default database and Chroma paths are under `backend/data`;
set `SQLITE_PATH` and `CHROMA_PATH` in the process environment to use other
locations. Back up the database before upgrades and verify the startup logs
and `/ready` endpoint after migration. Fresh and legacy schema migration tests
are included in the Python suite.

## Tests and checks

Run from the repository root with the virtual environment activated and project dependencies installed:

```sh
python -m pytest -q --tb=short
for file in frontend/js/*.js; do node --check "$file"; done
python -m compileall -q backend main.py start.py
python -m pytest -q backend/tests/test_m10_m11.py::test_fresh_and_legacy_database_migrations_add_role_and_operational_tables --tb=short
python -m pytest -q backend/tests/test_authorization_isolation.py --tb=short
```

Node runs the repository's focused JavaScript UI suites. The Python suite includes offline provider stubs; passing it does not certify
live provider credentials, quotas, or network access. Exercise real provider
integrations separately in a controlled environment with valid credentials.
Usage reservations count the serialized visible prompt representation with a
prompt-size safety margin and add the configured maximum output allowance;
they also use approximate model prices. Actual
provider tokenization, hidden request overhead, price changes, and billed costs
can exceed those estimates; a monthly spend cap is therefore a preventive
budget control, not a guaranteed hard spend ceiling. Unknown model pricing is
blocked when a spend cap is configured.

## Updated local setup (Atlas)

1. Use Python 3.11+ and create/activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set `GROQ_API_KEY`, `TAVILY_API_KEY`, and a strong random `SECRET_KEY` (at least 32 characters). Never commit `.env`.
4. The default Groq model is `openai/gpt-oss-120b`. The interactive Playground no longer offers Groq's retired `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` IDs. Explicit stale selections are not silently rerouted.
5. Start with `python start.py`; the frontend is served at `http://localhost:5500` and the API at `http://localhost:8000` (see startup output for exact endpoints).

Research report generation now allows a larger completion budget and makes one bounded, evidence-preserving recovery call when structured output is malformed or truncated. If recovery still fails, the app reports a clear actionable error rather than trying to guess-repair JSON.

## Batch 1 — Playground and Knowledge quality

The Playground uses a light, high-contrast content surface distinct from Atlas's
navy navigation chrome, with responsive model/source cards, visible keyboard
focus, reduced-motion support, and explicit per-model states (complete, provider
failure, timeout, and account limit). A failed model remains visible beside any
successful model output; explicit model selection is not silently substituted.

The conservative Knowledge extractor no longer promotes a standalone month
name to a proper-noun entity, while retaining month words in multi-token names
(e.g. an organization name) and retaining meaningful acronyms such as CAGR.
Knowledge rows that use button semantics can also be activated with Enter or
Space. Existing user-scoped IDs, evidence URL validation, provenance links, and
persistence rules remain in place.

Batch 1 verification: run `python -m pytest -q backend/tests/test_batch1_regressions.py`,
`node backend/tests/js/knowledge_ui.test.js`, `node backend/tests/js/routing_ui.test.js`,
`node backend/tests/js/atlasbench_ui.test.js`, `for file in frontend/js/*.js; do node --check "$file"; done`,
and `python -m compileall -q backend main.py start.py`. Full API/integration and
interactive-browser checks require the project's declared Python dependencies
(in particular `langgraph` and `langchain-core`) and a browser runtime; the
Batch 1 handoff records which checks were unavailable in the build environment.

## Workflow Studio (minimal supported slice)

The authenticated `/api/workflows` API and Workflow Studio UI support saving user-owned definitions from the built-in **Text cleanup** and **Text validation** templates, running deterministic text steps, inspecting step outcomes, and viewing persisted run history. Supported steps: `input_text`, `normalize_whitespace`, `append_text`, `uppercase`, and `require_contains`. Unknown steps are rejected server-side. Execution requests require an `Idempotency-Key` header; reusing the key with a different input returns HTTP 409. See `WORKFLOW_STUDIO_HANDOFF.md` for test results and environment limitations.

## Frontend (Archive 09 redesign)

Static files, no build step. Three separate surfaces:

| Page | Purpose |
|---|---|
| `frontend/index.html` (+ `terms.html`, `privacy.html`, `contact.html`) | Public marketing site. No workspace code. |
| `frontend/auth.html` | Sign in, sign up, forgot and reset password. Same endpoints and session storage as before. |
| `frontend/app.html` | Authenticated workspace. Redirects to `auth.html` when there is no valid session. |

`landing.html` remains as a redirect to `index.html` so old links keep working. Styles: `css/tokens.css` (palette tokens and shared
primitives), `css/site.css` + `css/landing.css` (public pages), `css/atlas.css` (workspace). The palette is exactly five inks —
`#F4EFE6`, `#FFFCF7`, `#302522`, `#78434A`, `#C9B9AD` — and every other tone is a `color-mix()` of those;
`backend/tests/test_archive09_palette.py` fails on any other colour or on `backdrop-filter`.

Set `window.ATLAS_CONTACT_EMAIL` in `frontend/js/config.js` to show a contact address on `contact.html`.
Checks: `node backend/tests/js/<name>.test.js` (module tests) and `cd frontend && npm install && npm test` (jsdom smoke test, dev-only).


## Frontend (meadow redesign)

The static frontend was redesigned around the meadow-at-sunset photograph: `index.html` (landing),
`auth.html` (sign in / sign up / password reset), `terms.html`, `privacy.html`, `contact.html` and the
workspace `app.html`. No build step. Backend, APIs, routes and authentication are unchanged.
See `FRONTEND_REDESIGN_REPORT.md` for what changed and exactly what was tested.

Frontend checks (no real backend required):

```sh
for f in frontend/js/*.js; do node --check $f; done     # syntax
for f in backend/tests/js/*.test.js; do node $f; done   # pure-function UI tests
pip install playwright && playwright install chromium
python frontend/tests/e2e/test_frontend_e2e.py          # browser e2e against a mock API
```
