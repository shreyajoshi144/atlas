# Operations runbook

For whoever deploys or supports Atlas for a team or customer. Everything here uses behaviour that exists in the code.

## 1. Pre-deployment checklist

| Check | How |
|---|---|
| Production mode on | `APP_ENV=production` (the Dockerfile sets it) |
| Strong signing secret | `SECRET_KEY`, 32+ random characters: `python -c "import secrets;print(secrets.token_urlsafe(48))"`. Startup fails on placeholders. |
| Exact CORS origins | `CORS_ALLOWED_ORIGINS=https://your-host`. Wildcards are rejected in production. Not exercised on single-origin hosting, but still required. |
| Per-user spend cap | `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` (startup fails without it) |
| App-wide spend cap | `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD`. Set it for any public deployment. |
| Provider keys in the secret store | `GROQ_API_KEY`, `TAVILY_API_KEY`, optional OpenAI, Anthropic, Gemini and xAI keys |
| Persistent storage | `ATLAS_DATA_DIR` on a durable volume. Free Render and Hugging Face disks are wiped on restart; use a paid persistent disk or storage add-on and set `ATLAS_DATA_DIR` to it (see the [deployment guide](07-deployment.md)). |
| Error tracking (optional) | `SENTRY_DSN`. No PII or request bodies are sent. |
| First admin | Sign up normally, then promote that account (section 3) |

## 2. Is it healthy?

| Probe | Meaning |
|---|---|
| `GET /ready` | `200` means config loaded and the database answered `SELECT 1`. `503` means the database is the problem. Used by the container `HEALTHCHECK`. |
| `GET /health` | `ok`, or `degraded` when the local embedding model isn't loaded. Degraded means chat and discovery lose semantic context, but the API is up. |
| `GET /api/admin/metrics` (admin) | Uptime, request totals, 4xx and 5xx counts, and p50 and p95 latency per route. A rising `error_rate_5xx` is the first signal. Per process, resets on restart. |
| `GET /api/admin/monitoring?days=30` (admin) | Model usage, cost estimates, failures and fallbacks over a window |
| `GET /api/admin/audit` (admin) | Immutable log of admin actions |

Every response carries `X-Request-ID`. Ask a user for it, then search the JSON logs for that value to find the full request trail.

## 3. Common admin tasks

**Promote an operator.** Roles are never changed through the API. On the host's SQLite file:

```sql
UPDATE users SET role = 'admin', updated_at = CURRENT_TIMESTAMP WHERE email = 'operator@example.com';
```

**Change one user's limits.**

```sh
curl -X PUT "$HOST/api/admin/users/$USER_ID/limits" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"daily_call_limit": 200, "monthly_spend_cap_usd": 10}'
```

**Rotate a provider key.** Update the secret, restart the service. Keys are read at startup and never returned by any endpoint.

**Rotate `SECRET_KEY`.** This signs out every user, because all existing tokens stop verifying. Do it if the key may have leaked.

**Back up.** Copy `data/sqlite/atlas.db` while the service is stopped, or use `sqlite3 atlas.db ".backup backup.db"` while it runs. Back up `data/chroma` too: it holds the embedded chunks used for semantic search, and I haven't verified a rebuild path from the database alone.

## 4. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Service won't start, message names a `ConfigurationError` | A production rule failed: weak `SECRET_KEY`, wildcard or missing CORS origins, or no spend cap | Fix the named variable. The message says which. |
| Users get "Daily model call limit reached" | Per-user daily limit (default 1000 events) | Raise that user's limit, or wait for the UTC day to roll over |
| "Monthly model spend cap would be exceeded" | Per-user monthly cap | Raise that user's cap, or wait for the new month |
| "The service has reached its monthly usage limit" for everyone | The global cap is spent | Raise `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD` and restart, or wait |
| "cost cannot be safely estimated" | A spend cap is set and the model has no price in the catalog | Use a catalogued model, or add a price. Unknown cost is blocked on purpose. |
| `/health` says `degraded` | Embedding model failed to load | Check the logs. In the Docker image the model is baked in, so look for a disk or memory problem. |
| Compare shows a provider as "not configured" | Missing or placeholder API key | Add the key. Placeholders such as `your-key-here` count as unset. |
| Provider calls fail with 429 or timeouts | Provider rate limiting or an outage | Atlas retries with backoff and trips a per-provider circuit breaker. Check the Compare run details for each attempt. Routing fallback only helps if a fallback chain is configured. |
| Login or signup returns 429 | Auth rate limit (login 10 per minute per IP) | Wait a minute. Behind a proxy, check `FORWARDED_ALLOW_IPS` so clients aren't all seen as one IP. |
| Users lose data after a restart, or sign-in says "invalid" for an account made earlier | Ephemeral disk: the host restarted (deploy, idle spin-down, or killed for memory) and the database was reset | Move `ATLAS_DATA_DIR` to a persistent volume. On a free host set `DEMO_EMAIL` and `DEMO_PASSWORD` so one login always exists, and keep the instance awake with an uptime monitor on `/ready`. |
| The hosted app restarts during a research run (Render log shows `Instance ... restarted`) | Out of memory on a 512 MB instance | Keep small-host mode on (the Docker image sets it). Do not unset `ATLAS_LIGHT_MODE`, `ATLAS_DENSE_RANKING` or `ATLAS_MAX_TOP_K` on a 512 MB host. See [Small-host mode](07-deployment.md#small-host-mode-512-mb). |
| First request after idle takes about a minute | Free instance was asleep | Wait, or keep it awake with an uptime monitor |
| Ask Atlas and Discover find nothing for new reports on the hosted demo | `ATLAS_LIGHT_MODE=1` skips vector indexing | Expected on the small host. Unset it on a larger host. |
| `/docs` returns 404 in production | API docs are off in production | Set `ENABLE_API_DOCS=true` if you want them |
| Research run fails mid-way | Search or scrape failure, or the writer returned bad output | The writer gets one bounded recovery call. Otherwise the run reports a clear error. Check logs by request ID. |

## 5. Onboarding a new team

1. Deploy with the checklist above and confirm `/ready`.
2. Create the first account and promote it to admin.
3. Set per-user limits sized to the team, and confirm the global cap.
4. Give each person their own account. Data is isolated per user, so there are no shared workspaces.
5. Run an AtlasBench benchmark on the models they plan to use, then decide between an explicit model and autopilot routing.
6. Watch `/api/admin/metrics` and `/api/admin/monitoring` for the first week.

## 6. Known limits to tell a customer

- On a free host the disk is wiped at every restart, and a 512 MB host runs in small-host mode (see the [deployment guide](07-deployment.md)).
- One instance and one worker. Rate limits and metrics are per process and reset on restart.
- SQLite plus local Chroma, so this isn't for horizontal scaling.
- Cost figures are estimates from catalog prices and token counts, not invoices. Spend caps are not hard ceilings.
- Tokens are signed but there is no revocation list. Rotating `SECRET_KEY` is the way to invalidate all sessions.
- There is no email provider, so password-reset links are logged, not emailed.
