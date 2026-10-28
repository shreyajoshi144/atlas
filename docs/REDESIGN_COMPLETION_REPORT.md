# Atlas — Archive 09 redesign (round 2): completion report

The original `Atlas_Redesigned_Release.zip` is untouched. Backend code (`backend/`, `main.py`, routes, auth, DB, integrations)
is **unchanged**. Non-frontend edits: `start.py` (now opens `index.html`/`app.html` and checks the new files), `README.md`, and test files (listed below).

## Information architecture
Three separate surfaces, each with one job:

| Surface | Files | Job |
|---|---|---|
| Public site | `index.html`, `terms.html`, `privacy.html`, `contact.html`, `css/site.css`, `css/landing.css`, `js/landing.js` | Explain Atlas and convert. No workspace or dashboard code. |
| Authentication | `auth.html`, `js/auth-page.js` | Sign in, sign up, forgot and reset password. Same endpoints, payloads and session storage as before. |
| Workspace | `app.html`, `css/atlas.css`, `js/*.js` | Six areas on a top bar: Overview, Research, AI Playground, AtlasBench, Workflow Studio, Runtime & Control. Research has a sub-bar: New case, Cases, Library, Discover, Knowledge map, Insights. Settings and sign-out live in the account menu. |

`landing.html` is now a redirect to `index.html`, so old links work. `css/style.css` is gone; shared tokens are in `css/tokens.css`.

## What is genuinely new in this round
* **Landing page** — new long-form editorial page: hero with an interactive claim→source specimen (clearly labelled illustration, sample text, no figures), how it works, four product areas each with its own schematic figure, features, use cases, security & trust, FAQ, final CTA, full footer (product, learn, account, Terms, Privacy, Security, Contact). Every claim on it was checked against the code; the security section lists behaviour, not certifications, and says what it does **not** claim.
* **Legal & contact pages** — Terms and Privacy are **templates** that describe real software behaviour and say plainly they are not legal advice and must be reviewed by the operator. Contact shows `window.ATLAS_CONTACT_EMAIL` (set in `js/config.js`); if unset it says no address is configured. Nothing is invented.
* **Auth page** — inline per-field validation matching backend rules (name ≤120, email, password 8–128, confirmation), loading and error states, 429 rate-limit message, show/hide password, "Keep me signed in", forgot + reset flows, `?reset_token=` and `#forgot` deep links, safe `next` redirect (same-app hash routes only), expired-session and signed-out notices. Password reset is described honestly: the backend only returns a token in development and there is no email provider in the repo.
* **Workspace shell and Overview** — new top-bar navigation, hash routing, mobile drawer, `aria-current`. Overview is deliberately small: greeting, **one** primary action, recent activity merged from real cases and Playground conversations (with loading, empty and error states), and one line per area on where to begin. The old checklist, pillars and composer are gone.
* **Research** — creation moved here: question field, five modes (source targets 8/12/14/20/20 as before), options, context. The old animated step ladder was **simulated** (a timer, not real progress); it is replaced with an elapsed clock and an honest "no live progress" note. Error state keeps the question.
* **Runtime & Control** — added a "Who answered" model-mix ribbon computed from the real usage summary, with a text legend (identity never depends on colour). Tested in `routing_ui.test.js`.
* **Fixes** — boot race (session restore ran before view scripts existed); AtlasBench intro guard; contrast of the mid tone (now 5.28:1); auth-page heading structure and an unlabeled field found by the new accessibility checks.

## What is NOT redesigned (read this)
The following screens still use their **original render functions and markup**, styled by the new tokens/stylesheet but not re-architected: **AI Playground, AtlasBench, Knowledge map, case/report workspace, Cases, Library, Discover, Insights, Settings, Ask Atlas widget**, and the remainder of Runtime & Control (policies, preview, usage table). Workflow Studio's markup was rewritten in the previous round. These are fully functional and pass their tests, but this does **not** meet "give every feature its own task-appropriate interface". That remains the largest piece of unfinished work, and each module has a Node test pinning its internals that would need to be migrated alongside a rewrite.

## Tests (all run in this environment)
| Check | Result |
|---|---|
| Node module suites: atlasbench 25, knowledge 34, routing 14, playground 12, workflow studio 8 | all pass |
| Frontend smoke (`frontend/tests/smoke.test.js`, jsdom, fake API): landing, auth validation/errors/success/recovery, session guard, Overview, navigation across all areas, research success + failure paths, accessibility basics on 6 pages | 97 / 97 |
| Full Python suite, original tree | 1078 passed, 18 failed, 2 skipped |
| Full Python suite, this tree | 1082 passed, 18 failed, 2 skipped — the **same 18** failures |
| Mutation checks | smoke suite fails when the password rule, sub-bar rendering or session guard is broken |
| Palette guard | passes; fails if any non-palette hex, `backdrop-filter` or colour function appears |
| Contrast | every text pair in use ≥ 4.5:1 |

The 18 failures are pre-existing and unrelated to the frontend (retired Groq model names in fixtures, a missing `m5_retrieval_cases.json`, stale monkeypatch targets). They were not touched.

**Test files changed (disclosed):** `test_m6/m8/m9_frontend.py`, `test_playground.py` now read `app.html`; `test_m6` asserts Knowledge is a Research sub-tab instead of a second nav item; `test_batch1_regressions.py`/`test_m9`/`test_playground` read `atlas.css`; two assertions that pinned the old blue hex values now assert the same rules with tokens; `test_archive09_palette.py` extended (public pages separate from the workspace). One test (`test_research_depth`) caught a regression in my first Research rewrite; the markup was restored rather than the test relaxed.

## Not verified
* **No visual QA.** No browser exists here, so nothing was looked at in Chrome/Safari/Firefox: layout, spacing, the responsive breakpoints, the SVG figures and the specimen interaction are untested visually. Expect CSS fixes on first viewing. jsdom checks wiring, states and accessibility basics, not appearance, and does not run CSS layout.
* **Fake API only.** The smoke test never talks to the real backend. The packaged backend was booted (`/health` 200), but no real sign-in → research → Playground run was executed. Real research needs provider keys; embeddings (`sentence-transformers`) were not installed, so `/health` reports degraded here.
* Fonts load from Google Fonts; offline, system fallbacks apply.
* Terms and Privacy need review by the operator/counsel before real users.
* Keyboard navigation of the legacy views, and screen-reader behaviour, were not audited manually.
