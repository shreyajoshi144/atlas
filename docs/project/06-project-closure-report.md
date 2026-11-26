# Project closure report

| | |
|---|---|
| Project | Atlas |
| Owner | Shreya Joshi |
| Period | 2026-07-02 to 2026-11-25 |
| Final release | 2.1.1 (2026-11-25) |
| Closure status | **Closed with open items.** The product is released and deployed. Full acceptance waits on backlog item B-01. |
| Related | [Charter](00-project-charter.md) · [PRD](01-prd.md) · [QA report](05-qa-report.md) · [Risks](04-risk-register.md) |

## 1. Summary

Atlas turns a question into a research report in which every claim links to its source and carries a verification label, and it measures the models behind that research with deterministic, repeatable tests. It was built by one person over about five months, hardened through eleven milestones and several bounded repair batches, deployed in a single container, and documented. The first public deployment exposed hosting problems that local tests could not, which were then fixed and the scope reduced to four areas.

## 2. Objectives against outcomes

| ID | Objective | Outcome | Evidence |
|---|---|---|---|
| O1 | Cited report with claim-level verification | **Met** | FR-01 to FR-04 tests pass |
| O2 | Measurable model quality without an LLM judge | **Met** (built and tested; no live-model results) | AtlasBench: 8 tasks, 6 measures; D-01 |
| O3 | Control cost before and during calls | **Met** | Estimate before run, atomic reservation, caps; NFR-04 |
| O4 | Per-user isolation and security | **Met** | Isolation, auth and security suites; secret scan |
| O5 | Deployable from the documentation | **Met** | One container; [deployment guide](../07-deployment.md); live on Render |
| O6 | Claim only what was verified | **Met** | Not-verified lists; no rankings shipped |
| O7 | A working public demo | **Partly met** | Landing, pages and probes verified on the live site; sign-up, sign-in and research verified locally after the fixes; hosted confirmation is B-01 |

## 3. Deliverables

| Deliverable | Status |
|---|---|
| Application: FastAPI API (89 operations), LangGraph research pipeline, static frontend with four areas | Delivered |
| Evaluation: AtlasBench, Compare grounding suite, routing and budgets | Delivered |
| Container image (amd64 and arm64) and CI | Delivered |
| Documentation set (`docs/`, nine numbered documents plus changelog) | Delivered |
| Management pack (this folder) | Delivered |
| Live demo: https://atlas-cite-check.onrender.com | Delivered; hosted confirmation open |

## 4. Scope changes

| Change | Release | Decision |
|---|---|---|
| Workflow Studio removed | 2.1.1 | [D-12](07-decision-log.md) |
| Runtime & Control folded into AtlasBench | 2.1.1 | [D-13](07-decision-log.md) |
| Small-host mode added | 2.1.1 | [D-11](07-decision-log.md) |
| API docs off in production | 2.1.1 | [D-14](07-decision-log.md) |
| Discover page removed | after 2.1.1 | [D-19](07-decision-log.md) |
| AtlasBench Compare tab removed | after 2.1.1 | [D-20](07-decision-log.md) |
| Compare merged into the AI Playground; Groq and free-tier Gemini added | after 2.1.1 | [D-18](07-decision-log.md), [D-21](07-decision-log.md) |

## 5. Schedule

Delivered in releases 1.0.0 (2026-07-02), 2.0.0 (2026-10-28), 2.1.0 (2026-11-23) and 2.1.1 (2026-11-25); see the [roadmap](02-roadmap-and-milestones.md). No baseline schedule was set at the start, so no schedule variance is reported.

## 6. Quality

Backend 1,245 passed and 4 skipped; provider suite 121 OK; frontend unit tests 7 files pass; browser tests 42/42 and 113/113 on 2026-11-23 and not re-run since. Nineteen defect groups were logged; all are closed except DEF-14, which is fixed locally and awaits hosted confirmation (DEF-18 and DEF-19 were verified locally and also need a hosted check). Verdict: **conditional pass** ([QA report](05-qa-report.md)).

## 7. Risks and issues at closure

| Open item | Reference |
|---|---|
| Hosted demo not re-confirmed after the fixes | R-01, B-01 |
| Free hosting wipes data at restart | R-02 (mitigated by the demo account) |
| No live-provider or model-quality results | R-05, B-02 |
| Proxy time limit on slow hosts | R-03, B-06 |
| Dependency advisories needing major upgrades | R-07, B-07 |
| Personas not validated with users | R-09, B-10 |

## 8. Lessons learned

| What went well | What to improve |
|---|---|
| Writing requirements and tests for each milestone made later hardening safe | The first public deployment came late; a hosting-sized test (512 MB, slow CPU) would have found the crash earlier |
| Recording "not verified" instead of "passed" kept every claim honest | The project had no baseline schedule or effort tracking, so progress could not be measured against a plan |
| Security and cost controls were built in, not added later | Documentation drifted (route counts, areas); one facts table and a search-before-release now guard against it |
| Cutting scope (Workflow Studio, Runtime & Control) made the product explainable | Scope was cut after release, not before; features should pass "can I demonstrate and explain it?" at design time |
| Deterministic evaluation made results reproducible | Live-provider validation was left to the end and then blocked by lack of credit; schedule a small funded run early |

## 9. Handover

| Item | Where |
|---|---|
| Source and history | https://github.com/shreyajoshi144/atlas, branch `main` |
| Run and deploy | [Deployment guide](../07-deployment.md), [Runbook](../06-runbook.md), [Configuration](../09-configuration.md) |
| What is next | [Backlog](03-product-backlog.md) and [Roadmap](02-roadmap-and-milestones.md) |
| Known limits | [PRD section 8](01-prd.md#8-known-limitations) |

## 10. Acceptance

| Role | Name | Decision |
|---|---|---|
| Project owner | Shreya Joshi | Release 2.1.1 accepted for demonstration, subject to B-01 |
