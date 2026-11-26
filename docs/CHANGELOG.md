# Changelog

All notable changes to Atlas. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release dates are the dates of the merging commits in Git. Earlier work was published as a few large commits, so the earlier entries group several milestones. See [development history](05-development-history.md) for detail.

## [2.1.1] - 2026-11-25

Fixes found by running the live deployment on Render's free tier (512 MB memory, a fraction of a CPU).

### Added
- Small-host mode, set by the Dockerfile: `ATLAS_EMBEDDING_BACKEND=onnx` (ONNX build of the same embedding model, no PyTorch), `ATLAS_DENSE_RANKING=0` (keyword and credibility ranking only), `ATLAS_MAX_TOP_K=6` (at most 6 sources per run) and `ATLAS_LIGHT_MODE=1` (no executive brief, no vector indexing; the report is still saved). Details in the [deployment guide](07-deployment.md#small-host-mode-512-mb).
- `ENABLE_API_DOCS` setting. The Swagger UI, ReDoc and `/openapi.json` are now off in production unless it is `true`. Tests in `backend/tests/test_api_docs_toggle.py`.
- "How it works" panels on AtlasBench and the AI Playground.
- Render deployment instructions, and runbook entries for hosted restarts.

### Changed
- AtlasBench is the single home for model scoring and routing: **Runtime & Control** is no longer a separate area; its content is the **Routing & cost** tab of AtlasBench.
- The workspace menu is Overview, Research, AI Playground, Compare and AtlasBench. The landing page, the Overview "Where to begin" list, the README and the docs describe the same five areas.
- Browser pages that are restored from the back/forward cache reload, so a signed-out user's page is not shown again.

### Fixed
- Research failed on the 512 MB host: one run peaked at about 870 MB locally, and the hosted app was restarted mid-run. A run now peaks at about 210 MB locally (measured before light mode was added).
- CI: the provider test step had no `GROQ_API_KEY` or `TAVILY_API_KEY` in its environment, so 46 tests errored in setup. The step now sets test values.

### Removed
- **Workflow Studio**: the `/api/workflows` routes (10 operations), the `backend/workflows` module, its three database tables in a fresh schema, its page, styles, tests and every mention in the landing page and docs. The API now has 89 operations.

## [2.1.0] - 2026-11-23

Production polish, merged as pull request #1.

### Added
- Documentation: docs index, [deployment guide](07-deployment.md), [API reference](08-api-reference.md) (generated from the OpenAPI schema) and [configuration reference](09-configuration.md).
- `FRAME_ANCESTORS` setting (default `'none'`). `deploy/start_space.sh` sets it to the Hugging Face origins so the Space page can embed the app; API responses stay non-embeddable. Validated at startup.
- `deploy/space/README.md`: Hugging Face Space front matter (`sdk: docker`, `app_port: 7860`).
- GitHub Actions CI: compile check, Python suite, provider suite, Node syntax checks and UI tests.
- `requirements-dev.txt` for test dependencies.
- `.dockerignore`, `.gitattributes`, and a container `HEALTHCHECK` on `/ready`.
- `FORWARDED_ALLOW_IPS` setting for the Space start script.
- `backend/tests/js/script_safety.test.js`: regression tests for link safety.
- Documentation set in `docs/`: product overview, architecture, evaluation, testing and results, development history.
- `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD`: an app-wide monthly spend ceiling across all accounts, enforced atomically in the same transaction as the per-user checks. Unpriced calls are refused while it is set. Tests in `backend/tests/test_global_spend_cap.py`.
- Request metrics: in-process counts, 4xx and 5xx errors and p50 and p95 latency per route template, exposed to admins at `GET /api/admin/metrics`. Tests in `backend/tests/test_metrics.py`.
- Optional Sentry error tracking through `SENTRY_DSN`, with PII and request bodies disabled and request data scrubbed. `sentry-sdk` added to `requirements.txt`.
- `docs/06-runbook.md`.
- Landing page: an **Engineering** section with figures counted from the repository (application lines, test functions, benchmark measures, providers), four short claims about how the system is built, links to the source and docs, and a plainly stated limits note. Added to the header and mobile navigation.

### Changed
- Workspace and sign-in pages restyled to match the landing page: sans-serif type throughout, raised card surfaces, a dark active-navigation pill, white bordered form controls, and a product panel beside the sign-in form (sample figure, labelled as sample data). The serif display font is no longer loaded on any page. All new colours derive from the existing eight-colour palette (a lighter tint of ivory defined once as `--card`), and no glass effects are used, as the palette tests require.
- Workspace Overview: a row of figures computed from data the screen already loads (research cases, Playground conversations, average verification of the latest cases).
- Landing page redesigned as a product page: centred hero over the meadow photograph, a dashboard preview built in HTML/SVG (sample data, labelled), card-based sections, a frosted header and sans-serif headings. Same sections, links and content; the grass and floating-particle decorations were removed.
- `lxml` requirement raised to `>=6.1,<7` (XML entity-resolution fix; Atlas only uses it as an HTML parser).
- `start.py` honors the `PORT` environment variable and serves the API and frontend from one process; README corrected to match.
- Twelve unused imports removed from production code.
- `pytest` and `pytest-asyncio` moved out of `requirements.txt`, so the production image no longer installs them.
- Six silent `except: pass` blocks now log a warning with the exception type, or catch only the exception they expect (`research`, `search`, `history` routes, `retrieval_service`, Compare usage logging).
- README: status section rewritten, documentation links updated.

### Fixed
- Sign-up and sign-in forms: validation messages appearing and disappearing shifted the form between mouse-down and mouse-up, so the first click on the submit button could be lost. Space for each message is now reserved.
- Leaving the Cases list while it was loading threw `Cannot set properties of null (setting 'innerHTML')` from the history view's error handler.
- "Keep me signed in" unchecked was ignored once the workspace loaded: the session refresh re-saved it to `localStorage` (default `remember = true`), so it outlived the tab. The refresh now keeps the stored choice. Tests in `backend/tests/js/session_persistence.test.js`.
- API base URL: a stale `atlas_api_base` value in `localStorage` (for example `http://localhost:8000/api` from an old dev session) overrode the same-origin default and was blocked by `connect-src 'self'`, so sign-up and sign-in failed with "Could not reach the server". Both pages now resolve the base through one function in `js/config.js`; only `ATLAS_API_BASE` in that file can change it, and the old key is removed on load. Tests in `backend/tests/js/api_base.test.js`.
- Static files had no `Cache-Control`, so browsers kept old JS and CSS after a deploy. HTML, JS, CSS and JSON are now `no-cache` (revalidated with ETag) via `backend/core/static.py`.
- A `304 Not Modified` for a frontend page was given the API's `default-src 'none'` CSP, which the browser then applied to the page. It now gets the page CSP.
- `deploy/start_space.sh` had stray Python lines after `exec`; removed.
- Dockerfile: `WORKDIR` ran before `USER`, leaving the app directory root-owned so `data/` could not be created. Added pip retries and a longer timeout.
- Two tests asserted older LangGraph behavior and failed on 0.6.11; they now check only what the app relies on.
- Workspace overview: a long case title could overlap its date and status in Recent activity at narrower widths. The meta column is now width-bounded and titles wrap.

### Removed
- Sixteen batch, sprint and handoff reports from `docs/`, superseded by the new documentation set.
- `backend/services/verification_service.py`: unused since `VerificationAgent` replaced it.

### Security
- Frontend: markdown links with `javascript:`, `data:` or other non-`http(s)` schemes in LLM-written reports are rendered as plain text. Source links in the case view, evidence list, lineage graph and Knowledge Map apply the same rule. One source link (`VIEW SOURCE`) was previously inserted without escaping.

## [2.0.0] - 2026-10-28

### Added
- Multi-provider Compare: OpenAI, Anthropic, Gemini and xAI, with a price catalog and a 24-case grounding suite.
- AtlasBench: frozen tasks, deterministic scoring, reproducible runs, output arbitration.
- Routing and Cost Autopilot with bounded fallback.
- Workflow Studio (removed in 2.1.1), per-user budgets, admin endpoints with an audit log.
- Argon2id password hashing with migration of legacy PBKDF2 hashes.
- Single-container deployment for Hugging Face Spaces.
- Redesigned static frontend: public site, auth page, workspace.

### Fixed
- HTTP 413 was retried as a rate limit. Only 429 is retried now.
- Provider API keys could appear in generic error strings. Log redaction now matches `gsk_`, `sk-proj-`, `sk-` and `tvly-` token shapes.
- The global error handler could return exception text and log query strings. Unexpected errors now return one generic message in every environment, and logs record path, method and exception type only.
- A router was not registered on the application.
- Exhausted AtlasBench runs could remain in a stale `running` state.
- Entity extraction treated standalone month names as entities.
- Playground cost labels could imply an unknown cost was free.

## [1.0.0] - 2026-07-02

### Added
- Atlas Living Research Workspace: LangGraph research pipeline, source ranking, claim verification, credibility scoring, executive brief, notes, chat and history, on FastAPI, SQLite and ChromaDB.
