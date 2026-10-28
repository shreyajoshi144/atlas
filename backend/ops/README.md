# M11 operations

Atlas persists explicit account roles, per-user limits, budget reservations, and an immutable
admin audit log in SQLite. New accounts and migrated accounts receive the `user` role. Promote an
operator using a controlled database administration process. For first-admin provisioning, create
the operator account through the normal signup flow, then use the deployment's secured SQLite
administration path to promote that account before exposing admin access. For example, with the
default database path:

```sql
UPDATE users SET role = 'admin', updated_at = CURRENT_TIMESTAMP WHERE email = 'operator@example.com';
```

Do not expose role changes through public registration or user-facing account APIs. Admin-only
endpoints are under `/api/admin`: `/monitoring`, `/users`, `/users/{user_id}/limits`, and `/audit`.
Every request checks the current database role server-side.

Production must set `APP_ENV=production`, a unique randomly generated `SECRET_KEY` of at least 32
characters, explicit non-wildcard `CORS_ALLOWED_ORIGINS`, and a nonnegative
`DEFAULT_MONTHLY_LLM_SPEND_CAP_USD`; startup fails if the spend cap is omitted. Configure
`GROQ_API_KEY` and `TAVILY_API_KEY` for model and search calls. Keep provider keys and signing secrets
in the deployment secret store. The cap is unset in development unless configured and uses
token-based estimates, which are not guaranteed hard spend ceilings.

`DEFAULT_DAILY_LLM_CALL_LIMIT` defaults to 1000 budget events per user per UTC day. A routed
request creates one event for its entire explicitly configured fallback chain, so fallback attempts
within that request count once; each AtlasBench `BudgetedLLM.invoke` creates one event. An
application retry that re-enters `invoke` creates another event. This is an invocation quota, not
a network-attempt counter. `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` applies the production default
monthly cap. An administrator can set an account override with
`PUT /api/admin/users/{user_id}/limits`. A configured spend cap blocks a model call if its price
cannot be estimated safely. Reservations include the configured output allowance and every
explicit fallback candidate. `reserved_cost_usd` preserves the original reservation for audit;
`estimated_cost_usd` records the settled charge used for future cap calculations. On completion,
unused fallback capacity is released. Successful attempts with reported usage are charged from
that usage; failed attempts and successful attempts without reported usage retain their full
per-attempt reservation. `actual_cost_usd` is calculated from provider-reported token counts and
catalog prices; it is a usage-derived estimate, not a provider invoice. Providers do not currently
report USD cost to Atlas. Token counts remain in their own columns, `actual_cost_usd` stores the
usage-derived estimate, and `estimated_cost_usd` stores the reservation/settled amount used for cap
enforcement. Costs above the reservation are charged at the greater value. Prompt reservations count
the serialized visible message representation at 1.25
tokens per character, add a fixed 256-token framing allowance, reserve the configured maximum output
tokens, and reserve the full possible explicit fallback chain. Unknown model prices fail closed when a spend cap
is active. These are still estimates, not guaranteed hard spend ceilings: provider tokenization,
hidden request framing, current prices, and actual billing can exceed the reservation. An overage
is recorded as the greater of estimate and reported actual cost and reduces the remaining budget
for subsequent calls, but cannot undo a cost already incurred. Expired in-flight reservations are
marked as unresolved failed calls after 30 minutes and remain in daily/monthly accounting at their
original reserved amount. This covers provider work that may have succeeded before an application
process crashed; a late completion can still reconcile the event.

Calls whose model price or provider usage is unknown are explicitly marked as unknown in
`llm_budget_events`; monitoring reports their cost as unknown rather than `$0`. They are
permitted only while no monthly spend cap is active. If a cap is enabled later in that
calendar month, new calls fail closed until the unknown cost is reconciled or the month
changes. Existing databases migrate conservatively: legacy zero estimates with no actual
cost are treated as unknown because older versions could not distinguish them from unpriced calls.

`/health` retains its existing embedding status. `/ready` checks database availability and returns
only a generic ready/unavailable status. The operational monitoring response is metadata-only and
does not include prompts, completions, secrets, or raw traces. Admin audit rows cannot be updated
or deleted through SQLite statements.
