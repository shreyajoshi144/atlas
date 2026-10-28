# Cost Autopilot + Model Routing (M9)

Choose which model answers each call, fall back when a provider fails, and record what happened.
Agents and the graph are **unchanged**: the router sits behind `core.llm.get_chat_llm()` and the
agents keep calling `invoke_with_retry(self.llm, messages)`.

## How a call is routed
| Scope of the call | Behaviour |
|---|---|
| none (legacy / background) | the application model, no fallback, nothing recorded — identical to pre-M9 |
| **AtlasBench run** | the benchmark config's model only: no autopilot, no fallback, not counted as production usage |
| user scope (research, chat) | the user's **active policy** picks the model; every call writes one `llm_usage` row |

The user comes from a `ContextVar` set by the entrypoint. LangGraph copies it into parallel nodes, but
`run_in_executor` does not — the research route uses `in_current_context(...)` (tested end to end).

## Policies (per user, persisted)
* **explicit** — always `model`.
* **autopilot** — the cheapest candidate meeting **all** of: quality ≥ `min_quality`, mean time per model
  call ≤ `max_latency_ms`, estimated cost of *this* call ≤ `max_cost_per_call_usd`. Quality and latency come
  from the user's own most recent usable AtlasBench run per model (same scorer version, fresh enough,
  enough cases; quality counts crashed tasks as 0). Cost = list price × estimated tokens of the call.
  **Unknown is never good enough**: no benchmark / no price / no latency ⇒ not eligible. If nothing
  qualifies (or there is no evidence) `model` is used and the recorded reason says exactly why.
* **fallback** — opt-in, an explicit ordered list of allowed models (max 3).
* No active policy ⇒ the application model, no fallback, calls still recorded.

## Fallback
Only for provider trouble: timeout/408, 429 (incl. rate-limit 413 bodies), 5xx, unreachable provider, a
withdrawn model. Never for 400/401/403/422, validation or parsing errors, or our own bugs — those propagate.
Never silent: the usage row has requested model, actual model, every attempt, and the reason; a warning is logged.
If every model fails, the original exception is re-raised unchanged (so `invoke_with_retry`'s existing
429 backoff still works). Never during AtlasBench runs, so comparisons stay valid.

## Recorded per call (`llm_usage`, no prompts or responses)
requested/actual model · mode · reason code + text · fallback used + reason · attempts · prompt/completion/total
tokens (provider-reported, else `null`) · **estimated** cost (before the call, requested model) · **actual** cost
(reported tokens × answering model's price, else `null`) · total and answering-attempt latency · evidence run id.

## API (`/api/routing`, authenticated, user-scoped)
`GET /overview` · `GET|POST /policies` · `DELETE /policies/{id}` · `PUT /active` · `POST /preview` (no model call) ·
`GET /usage` · `GET /usage/summary`. UI: **AtlasBench → Routing & cost**.

## Known limits
* Purpose granularity is per entrypoint (research / chat), not per agent — agents are deliberately untouched.
* Calls through `with_structured_output` (chat) report no token usage, so their actual cost is `null`.
* Failed attempts are not costed (providers generally don't bill 429/5xx); a timeout the provider did bill is not captured.
* Prices in `bench.models.MODEL_CATALOG` are estimates; autopilot decisions inherit that uncertainty.
* Autopilot latency is the benchmark's *mean* per call, not a percentile.
* Real provider failures were simulated; no live provider calls were made in development.
