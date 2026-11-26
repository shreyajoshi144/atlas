# API reference

Atlas serves a JSON API under `/api`, plus two health probes. This page lists every route (89 in total). It was generated from the application's OpenAPI schema; the live, interactive version (Swagger UI at `/docs`, schema at `/openapi.json`) is on by default in development and **off in production** unless `ENABLE_API_DOCS=true` is set.

## Conventions

| | |
|---|---|
| Authentication | `POST /api/auth/login` or `/register` returns `data.access_token`. Send it as `Authorization: Bearer <token>`. Tokens are HMAC-signed and expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 7 days). There is no refresh or server-side logout endpoint: signing out clears the token in the browser. |
| Success body | `{"success": true, "message": "...", "data": ...}` |
| Error body | `{"detail": "..."}` with `401` (missing or invalid token), `403` (not an admin), `404`, `409` (e.g. email already registered), `413` (body over `MAX_REQUEST_BODY_BYTES`), `422` (validation), `429` (rate limit or spend cap) or `500` (generic message; details go to the server log only) |
| Data scoping | Every user-owned resource is filtered by the authenticated user. Another user's id returns `404`, not the data. |
| Request ids | Every response carries `X-Request-ID`; the same id is in the server's JSON log lines. |
| Rate limits | In-memory, per process. Per IP for unauthenticated routes, per user for the rest. See the last column below. |
| Spend limits | LLM-calling routes also enforce per-user daily call limits and monthly spend caps, and an optional app-wide cap. See [configuration](09-configuration.md). |

Access levels: **Open** needs no token, **User** needs a valid token, **Admin** needs a token for an account with the admin role (promoted through the database; see the [runbook](06-runbook.md)).

## Routes

### health

Service health.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/health` | Health Check | Open |  |
| GET | `/ready` | Readiness Check | Open |  |

### auth

Accounts and sessions.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/auth/forgot-password` | Forgot Password | Open | 5 / 5 min / IP |
| POST | `/api/auth/login` | Login | Open | 10 / min / IP |
| GET | `/api/auth/me` | Me | User |  |
| POST | `/api/auth/register` | Register | Open | 5 / min / IP |
| POST | `/api/auth/reset-password` | Reset Password | Open | 10 / 5 min / IP |

### research

Run the research pipeline.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/research/run` | Run Research | User | 5 / 5 min / user |

### chat

Follow-up questions about a report.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/chat/research` | Research Chat | User | 20 / 5 min / user |

### history

Saved reports.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/history/report/{report_id}` | Get Report | User |  |
| GET | `/api/history/reports` | List Reports | User |  |

### knowledge-base

Library of past research.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/knowledge-base` | Knowledge Base Overview | User |  |
| DELETE | `/api/knowledge-base/report/{report_id}` | Delete Report | User |  |

### knowledge-map

Entities, claims and sources across reports.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/knowledge-map/changes` | Changes | User |  |
| GET | `/api/knowledge-map/claims` | Claims | User |  |
| GET | `/api/knowledge-map/claims/{claim_id}` | Claim | User |  |
| GET | `/api/knowledge-map/entities` | Entities | User |  |
| GET | `/api/knowledge-map/entities/{entity_id}` | Entity | User |  |
| GET | `/api/knowledge-map/evidence/{evidence_id}` | Evidence | User |  |
| GET | `/api/knowledge-map/graph` | Graph | User |  |
| GET | `/api/knowledge-map/investigations` | Investigations | User |  |
| POST | `/api/knowledge-map/investigations/{report_id}/rebuild` | Rebuild | User |  |
| GET | `/api/knowledge-map/sources` | Sources | User |  |
| GET | `/api/knowledge-map/sources/{source_id}` | Source | User |  |
| GET | `/api/knowledge-map/summary` | Summary | User |  |

### questions

Unanswered questions and research insights.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/questions/{report_id}` | Get Unanswered Questions | User |  |
| GET | `/api/questions/{report_id}/insights` | Get Research Insights | User |  |
| POST | `/api/questions/{report_id}/research` | Research From Question | User |  |

### notes

Per-report notes.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/notes/{report_id}` | Get Note | User |  |
| PUT | `/api/notes/{report_id}` | Save Note | User |  |
| DELETE | `/api/notes/{report_id}` | Delete Note | User |  |

### search

Semantic search across saved research.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/search/semantic` | Semantic Search | User | 30 / 5 min / user |

### playground

Side-by-side model chat.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/playground/chat` | Playground Chat | User | 20 / 5 min / user |
| GET | `/api/playground/conversations` | List Conversations | User |  |
| GET | `/api/playground/conversations/{conversation_id}` | Get Conversation | User |  |
| DELETE | `/api/playground/conversations/{conversation_id}` | Delete Conversation | User |  |
| GET | `/api/playground/models` | List Models | User |  |

### compare

Multi-provider comparison with grounding checks.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/compare/admin/discover` | Admin Discover | Admin | 6 / 5 min / user |
| GET | `/api/compare/admin/health` | Admin Health | Admin |  |
| GET | `/api/compare/admin/metrics` | Admin Metrics | Admin |  |
| POST | `/api/compare/ask` | Ask With Fallback | User | 6 / 5 min / user (shared) |
| GET | `/api/compare/evaluations` | Evaluations | User |  |
| GET | `/api/compare/models` | List Models | User |  |
| POST | `/api/compare/run` | Run Compare | User | 6 / 5 min / user (shared) |
| GET | `/api/compare/runs` | List Runs | User |  |
| GET | `/api/compare/runs/{run_id}` | Get Run | User |  |
| DELETE | `/api/compare/runs/{run_id}` | Delete Run | User |  |

### atlasbench

Reproducible benchmark: tasks, runs, arbitration.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/atlasbench/arbitrations` | Arbitrate | User |  |
| GET | `/api/atlasbench/arbitrations` | List Arbitrations | User |  |
| GET | `/api/atlasbench/arbitrations/{arbitration_id}` | Get Arbitration | User |  |
| GET | `/api/atlasbench/compare` | Compare Runs | User |  |
| GET | `/api/atlasbench/configs` | List Configs | User |  |
| POST | `/api/atlasbench/configs` | Create Config | User |  |
| DELETE | `/api/atlasbench/configs/{config_id}` | Delete Config | User |  |
| GET | `/api/atlasbench/overview` | Overview | User |  |
| POST | `/api/atlasbench/runs` | Create Run | User |  |
| GET | `/api/atlasbench/runs` | List Runs | User |  |
| GET | `/api/atlasbench/runs/{run_id}` | Get Run | User |  |
| DELETE | `/api/atlasbench/runs/{run_id}` | Delete Run | User |  |
| GET | `/api/atlasbench/runs/{run_id}/cases/{task_id}` | Inspect Case | User |  |
| POST | `/api/atlasbench/runs/{run_id}/execute` | Execute Run | User | 6 / 5 min / user |
| POST | `/api/atlasbench/runs/{run_id}/reproduce` | Reproduce Run | User |  |
| GET | `/api/atlasbench/tasks` | List Tasks | User |  |
| POST | `/api/atlasbench/tasks` | Create Task | User |  |
| POST | `/api/atlasbench/tasks/from-claim` | Task From Claim | User |  |
| GET | `/api/atlasbench/tasks/{task_id}` | Get Task | User |  |
| DELETE | `/api/atlasbench/tasks/{task_id}` | Delete Task | User |  |

### challenges

Single-claim verification challenges.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| POST | `/api/challenges` | Create Challenge | User |  |
| GET | `/api/challenges` | List Challenges | User |  |
| GET | `/api/challenges/{challenge_id}` | Get Challenge | User |  |
| POST | `/api/challenges/{challenge_id}/execute` | Execute Challenge | User | 10 / 5 min / user |
| GET | `/api/challenges/{challenge_id}/inspect` | Inspect Challenge | User |  |

### routing

Cost Autopilot routing policies and usage.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| PUT | `/api/routing/active` | Set Active | User |  |
| GET | `/api/routing/overview` | Overview | User |  |
| GET | `/api/routing/policies` | List Policies | User |  |
| POST | `/api/routing/policies` | Create Policy | User |  |
| DELETE | `/api/routing/policies/{policy_id}` | Delete Policy | User |  |
| POST | `/api/routing/preview` | Preview | User | 60 / 5 min / user |
| GET | `/api/routing/usage` | Usage | User |  |
| GET | `/api/routing/usage/summary` | Usage Summary | User |  |

### settings

Per-user settings.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/settings` | Get Settings Route | User |  |
| POST | `/api/settings` | Update Settings Route | User |  |

### analytics

Usage overview.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/analytics/overview` | Analytics Overview | User |  |

### admin

Operator endpoints.

| Method | Path | Summary | Access | Rate limit |
|---|---|---|---|---|
| GET | `/api/admin/audit` | Audit | Admin |  |
| GET | `/api/admin/metrics` | Request Metrics | Admin |  |
| GET | `/api/admin/monitoring` | Monitoring | Admin |  |
| GET | `/api/admin/users` | Users | Admin |  |
| GET | `/api/admin/users/{user_id}/limits` | Get User Limits | Admin |  |
| PUT | `/api/admin/users/{user_id}/limits` | Set User Limits | Admin |  |
