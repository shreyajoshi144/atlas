# Product requirements document (PRD)

| | |
|---|---|
| Product | Atlas 2.1.1 |
| Owner | Shreya Joshi |
| Related | [Charter](00-project-charter.md) · [Backlog](03-product-backlog.md) · [QA report](05-qa-report.md) |

## 1. Purpose

Define what Atlas must do, for whom, and how each requirement is checked. The status column describes the released product (2.1.1).

## 2. Users and needs

These personas are hypotheses (no interviews yet, backlog B-10).

| Persona | Job to be done | Pain today |
|---|---|---|
| **Analyst or researcher** | Reach a conclusion and defend it to a reviewer | Cannot tell which claims are supported without redoing the work |
| **Team choosing a model** | Pick the model that earns the work at an acceptable cost | Comparisons are opinions and demos, and costs surprise them |
| **Operator** | Run the service safely and cheaply | Unbounded spend, unclear failures, leaked keys |

## 3. User stories

| ID | As a | I want | So that |
|---|---|---|---|
| US-1 | analyst | to ask a question and get a report where each claim links to its source | I can check any claim in one click |
| US-2 | analyst | each claim labelled supporting, uncertain or conflicting | I know how much to trust it |
| US-3 | analyst | to challenge a finding with adversarial queries | I find out if it breaks before I rely on it |
| US-4 | analyst | to ask follow-up questions grounded in my saved research | answers stay tied to evidence |
| US-5 | model chooser | one question sent to several models, with cost, speed and citations | I can compare them on the same input |
| US-6 | model chooser | a repeatable exam with known answers | quality is measured, not argued |
| US-7 | model chooser | requests routed to the cheapest model that meets my quality bar | I control cost without losing quality |
| US-8 | operator | spend limits that apply before a call is made | one mistake cannot run up a bill |
| US-9 | any user | my data separate from everyone else's | nobody sees my research |
| US-10 | first-time user | a short explanation on each evaluation page | I understand what I am looking at |

## 4. Functional requirements

Priority uses MoSCoW. Verification names the automated tests that cover the requirement (all under `backend/tests/` unless stated).

| ID | Requirement | Priority | Verification | Status |
|---|---|---|---|---|
| FR-01 | A research run turns a question into a report through a 12-node pipeline: plan, search, scrape, evidence, verification, writer, credibility, brief, finalize | Must | `test_milestone1_e2e`, `test_research_depth`, `test_writer_recovery_and_model_ids` | Done |
| FR-02 | Every claim has a verification status and stays linked to its evidence | Must | `test_verification_agent`, `test_milestone1_e2e` | Done |
| FR-03 | Source ranking scores relevance separately from credibility | Must | `test_research_depth`, `test_publication_dates` | Done |
| FR-04 | Plans run only over a closed registry of 5 tools, with bounded re-planning, and fall back to the fixed pipeline if planning fails | Must | `test_research_planner`, `test_agentic_execution`, `test_agentic_integration`, `test_tool_registry`, `test_tool_registry_real_services` | Done |
| FR-05 | Saved research can be searched by keyword and meaning (BM25, dense, rank fusion, reranking, metadata filters) | Should | `test_m5_hybrid_retrieval`, `test_embedding_dependency` | Done (3 related tests skipped: missing fixture, B-11) |
| FR-06 | A per-user Knowledge Map records entities and claims with change tracking and freshness | Should | `test_m5_knowledge`, `test_m6_knowledge_map`, `test_m6_changes`, `test_m6_freshness`, `test_m6_api` | Done |
| FR-07 | Challenge mode runs six types of adversarial query with six result states | Should | `test_m7_challenges`, `test_m7_api` | Done |
| FR-08 | Notes, open questions and grounded chat attach to saved research | Should | `test_notes_and_questions` | Done |
| FR-09 | Users can browse Cases, Library, Discover and Insights | Should | `test_api_security`, browser e2e | Done |
| FR-10 | The AI Playground sends one question to up to three allowlisted models and shows answers, usage and cost | Should | `test_playground`, `backend/tests/js/playground_ui.test.js` | Done |
| FR-11 | Compare sends one question and the same evidence to up to four providers and records answer, citations, tokens, cost, latency, retries and failures | Should | provider suite (121 tests) | Done (never run against live providers) |
| FR-12 | AtlasBench runs 8 frozen tasks, scores 6 measures deterministically, supports reproducible and resumable runs, run comparison and arbitration | Must | `test_m8_core`, `test_m8_runner`, `test_m8_service`, `test_m8_api`, `test_m8_recovery_policy`, `test_m10_m11` | Done |
| FR-13 | Routing supports explicit, autopilot and fallback policies, managed on the Routing & cost tab of AtlasBench | Should | `test_m9_selector`, `test_m9_router`, `test_m9_service_api` | Done |
| FR-14 | Budgets: per-user daily call limit, per-user monthly spend cap, app-wide cap, all reserved before the call | Must | `test_batch3_runtime_controls`, `test_batch3_completion_controls`, `test_global_spend_cap` | Done |
| FR-15 | Accounts: register, sign in, signed expiring tokens, per-user data isolation | Must | `test_auth_security`, `test_authorization_isolation`, `test_api_security` | Done |
| FR-16 | Admin routes provide metrics, monitoring, audit log and limit controls | Could | `test_metrics`, `test_milestone3_observability_safety` | Done |
| FR-17 | Compare, the Playground and AtlasBench each explain how they work on the page | Should | `backend/tests/js/*` (UI modules); content added 2026-11-25 | Done |

## 5. Non-functional requirements

| ID | Requirement | Target | Verification | Status |
|---|---|---|---|---|
| NFR-01 | Security | Argon2id hashing; HMAC-signed tokens that expire (7 days by default); per-route rate limits; SSRF guard on page fetching; strict CSP; untrusted text never becomes a link unless `http(s)` | `test_auth_security`, `test_api_security`, `test_m4_security_reliability`, `backend/tests/js/script_safety.test.js` | Met |
| NFR-02 | Privacy and isolation | Every user-owned query filters on `user_id`; another user's id returns 404 | `test_authorization_isolation` | Met |
| NFR-03 | Reliability | Retry only on 429 with backoff and `Retry-After`; per-provider circuit breaker; fallback only on provider trouble, never on validation errors | `test_sprint4_runtime_safety`, provider suite | Met |
| NFR-04 | Cost control | Worst-case estimate before a run; budget reserved atomically before the call and settled afterwards; unknown cost is never shown as zero | `test_global_spend_cap`, `test_batch3_runtime_controls`, provider suite | Met |
| NFR-05 | Evaluation is deterministic | No LLM judge; the same input gives the same score | `test_m8_core`, `test_m10_m11` | Met |
| NFR-06 | Observability | Request id on every response; JSON logs; provider-key shapes redacted; per-route metrics | `test_metrics`, `test_milestone3_observability_safety` | Met |
| NFR-07 | Deployability | One container on port 7860, amd64 and arm64, health probes `/ready` and `/health`; runs in 512 MB in small-host mode | Docker builds and runs; hosted checks | Met locally; hosted research is open (B-01) |
| NFR-08 | Quality gates | CI runs compile check, backend suite, provider suite and frontend checks on every push | `.github/workflows/ci.yml` | Met |
| NFR-09 | Honest claims | Documentation lists what was not verified; no model ranking without data | [Testing and results](../04-testing-and-results.md) | Met |

## 6. Acceptance criteria

A release is acceptable when all of these hold.

1. The backend suite, provider suite and frontend unit tests pass, with skipped tests listed and explained.
2. A fresh container starts, `/ready` returns 200 and `/health` returns `ok`.
3. A new user can register, sign in, run one research case and find it in the Library.
4. A second user cannot read the first user's data.
5. A run that would exceed a spend limit is refused before any provider is called.
6. The documentation, landing page and application menu describe the same five areas.

Criteria 1, 2, 4, 5 and 6 were checked for 2.1.1. Criterion 3 was checked locally; the hosted check is open (B-01).

## 7. Out of scope

Document upload, horizontal scaling, email delivery, token revocation, voice, a mobile app, real-time collaboration, and Workflow Studio (removed, decision D-12).

## 8. Known limitations

| Limitation | Effect |
|---|---|
| Single instance, SQLite and local Chroma | Not for horizontal scaling. Rate limits and metrics are per process. |
| Free hosting wipes the disk on restart | Accounts and reports on the demo are temporary; a demo account is re-created at start |
| Small-host mode on the demo | Source ranking is less semantic; new reports are not vector-indexed, so Ask Atlas and Discover do not find them |
| Costs are estimates | Catalog list prices and token counts, not invoices |
| No live-provider results | The evaluation harness is tested against fake providers only |
