# Decision log

Each entry records what was decided, what else was considered, why, and what it costs. Decisions that cut scope are marked **Cut**. Dates are given only where they are known; earlier design decisions are tied to the release that carried them.

| | |
|---|---|
| Related | [Charter](00-project-charter.md) · [PRD](01-prd.md) · [Risks](04-risk-register.md) · [Architecture trade-offs](../02-architecture.md#9-design-decisions-and-trade-offs) |

## Decisions

| ID | Decision | Alternatives considered | Reason | Consequence |
|---|---|---|---|---|
| D-01 | Score models with fixed rules, **no LLM judge** (v2.0) | An LLM grader | Same input gives the same score, it is cheap, and every point can be explained | Lexical matching can miss a paraphrased quote |
| D-02 | SQLite plus an embedded ChromaDB, one instance (v1.0) | Postgres and a hosted vector store | Zero operations; one volume; easy to reason about | No horizontal scaling (backlog B-09) |
| D-03 | A closed registry of 5 tools and a deterministic template planner (M4) | An LLM that plans and calls arbitrary tools | Predictable, testable, and a model cannot call anything off the list | Adding a tool is a code change |
| D-04 | Put the model router behind `get_chat_llm` (M9) | Change every agent | Agents and the graph stay unchanged when routing changes | The user context must be carried explicitly into thread pools |
| D-05 | Argon2id passwords with migration of old PBKDF2 hashes; hand-signed expiring tokens (v2.0) | A standard JWT library | No extra dependency and easy to audit | No revocation (R-08, B-08) |
| D-06 | A static frontend with no framework (v2.0) | React or similar | No build step; one container | A large hand-written `script.js` (B-14) |
| D-07 | Ship **no model rankings**; show a ranking only when 95% Wilson intervals do not overlap (v2.0) | Ship sample results | A ranking without data is an opinion | There are no model results until B-02 |
| D-08 | Retry only HTTP 429; fall back to another model only on provider trouble (Sprint 4, M9) | Retry or fall back on any error | A 413 or a validation error is not fixed by retrying or switching models | Bugs surface instead of being hidden |
| D-09 | Serve the API and frontend from one origin in one container (v2.0) | Separate frontend host | No CORS in the deployed path; one thing to deploy | One process owns both |
| D-10 | Deploy the live demo on **Render's free tier** (2026-11-23) | Hugging Face Docker Space | Docker Spaces need a paid plan; Render builds the existing Dockerfile for free | Free instance: 512 MB, sleeps when idle, disk wiped at restart |
| D-11 | Add **small-host mode** to the Docker image: ONNX embeddings, keyword-only ranking, at most 6 sources, no vector indexing or executive brief (2026-11-25) | Pay for a bigger instance; shrink the product | Research needed about 870 MB and the free instance has 512 MB, so it crashed | Less semantic ranking; Ask Atlas and Discover do not find new reports on the demo; unset the variables on a larger host |
| D-12 | **Cut: remove Workflow Studio** (2026-11-25) | Keep it hidden | It supported none of the objectives and was hard to explain; every page has to be demonstrable and explainable | The `/api/workflows` routes (10 operations), module, tables, page, tests and all mentions were deleted; the API has 89 operations |
| D-13 | **Cut: fold Runtime & Control into AtlasBench** as the Routing & cost tab (2026-11-25) | Keep a separate area | Same backend as AtlasBench; one place is easier to understand | The menu has five areas, not seven |
| D-14 | Turn API documentation off in production unless `ENABLE_API_DOCS=true` (2026-11-25) | Leave it public | A public route list is free reconnaissance and not needed by visitors | Operators must opt in |
| D-15 | Re-create a demo account at every start from `DEMO_EMAIL` and `DEMO_PASSWORD` (v2.0; relied on from 2026-11-25) | Persistent disk (paid) | Gives reviewers a login that survives restarts | One shared account; other accounts are temporary |
| D-16 | Replace 16 batch and sprint reports with a numbered documentation set (v2.1.0) | Keep them | The reports contradicted each other and went stale | One set to maintain; this pack adds the management view |
| D-17 | Refuse to start in production without a strong secret, explicit CORS origins and a spend cap (v2.0) | Warn only | Unsafe defaults should fail loudly | A misconfigured deploy does not start |

## Open decisions

| ID | Question | Options | Needed by |
|---|---|---|---|
| D-18 | Should Compare run on the demo by adding Groq as a provider? | Add Groq (B-03); leave Compare as built and say it needs keys | Before the next release |
