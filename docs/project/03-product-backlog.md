# Product backlog

Prioritised with MoSCoW (Must, Should, Could, Won't now). Delivered work is recorded by its requirement ID in the [PRD](01-prd.md); this page lists what is still open and what was deliberately dropped.

| | |
|---|---|
| Related | [PRD](01-prd.md) · [Roadmap](02-roadmap-and-milestones.md) · [Risks](04-risk-register.md) |
| Last reviewed | 2026-11-25 (release 2.1.1) |

## 1. Open items, in priority order

| ID | Item | Why it matters | Priority | Size | Status | Linked |
|---|---|---|---|---|---|---|
| B-01 | Confirm sign-up, sign-in and one research run on the live site after the 2026-11-25 fixes | The public link is the product's first impression; the fixes were verified locally only | Must | S | **Open** | R-01, I-01, I-02 |
| B-02 | Run the grounding suite and AtlasBench on at least two live providers and publish pass rates with confidence intervals | Turns "built" into "measured"; today there are no model results | Must | M | Not started (needs API credit) | R-05 |
| B-03 | Add Groq as a Compare provider so Compare runs on the demo | Compare currently needs keys the demo does not have | Should | M | Proposed (decision pending) | I-04 |
| B-04 | Re-run the browser end-to-end suites after the menu change | They last ran on 2026-11-23, before the change | Should | S | Open (Playwright not installed on 2026-11-25) | [QA gaps](05-qa-report.md#6-what-was-not-checked) |
| B-05 | Persistent storage for the hosted demo | Accounts and reports are lost at each restart | Should | S | Not started (paid disk) | R-02 |
| B-06 | Run research as a background job with polling | A hosted proxy can cut a request at about 100 s; the free CPU is slow | Should | L | Not started | R-03 |
| B-07 | Major dependency upgrades (LangGraph 1.x, LangChain 1.x, Transformers 5, sentence-transformers 5) | Open security advisories, none with a known exposure in Atlas | Should | L | Not started | R-07 |
| B-08 | Token revocation or standard JWTs | Signed tokens cannot be revoked; rotating `SECRET_KEY` signs everyone out | Should | M | Not started | R-08 |
| B-10 | Interview five analysts to test the personas | The users in the PRD are hypotheses | Should | M | Not started | R-09 |
| B-09 | Postgres, a hosted vector store and a shared rate limiter | Needed before more than one instance | Could | L | Not started | R-06 |
| B-11 | Restore the frozen retrieval fixture (`backend/tests/data/m5_retrieval_cases.json`) | Three tests are skipped and the real-embedding quality benchmark is not running | Could | S | Not started | R-10 |
| B-12 | Document upload | Lets users research their own files | Could | L | Not started | |
| B-13 | Email delivery for password reset | Reset links are logged, not sent | Could | M | Not started | |
| B-14 | Split `frontend/js/script.js` (about 2,100 lines) into modules | Maintainability | Could | M | Not started | |

Size: S under a day, M a few days, L a week or more (estimates, not tracked effort).

## 2. Dropped

| Item | Decision | Reason |
|---|---|---|
| Workflow Studio (user-defined deterministic workflows) | Removed in 2.1.1, decision [D-12](07-decision-log.md) | Did not support any objective and was hard to explain |
| Runtime & Control as a separate area | Folded into AtlasBench in 2.1.1, decision [D-13](07-decision-log.md) | Same backend as AtlasBench; one place is easier to understand |
| Pre-computed model rankings | Never shipped, decision [D-07](07-decision-log.md) | No ranking is shown without data that supports it |

## 3. Delivered scope (summary)

All requirements FR-01 to FR-17 and NFR-01 to NFR-09 in the [PRD](01-prd.md) are delivered, with the status notes shown there.
