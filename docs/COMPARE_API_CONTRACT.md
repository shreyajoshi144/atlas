# Compare API contract (`/api/compare`)

All routes require `Authorization: Bearer <token>`. Envelope is the existing
`{success, message, data}` (`ApiResponse`). Errors use FastAPI's `{detail}`.
Nothing here ever returns a provider API key; `configured` is a boolean only.

## GET /models
`data`:
```
{
  providers: [{id, label, configured: bool, circuit: "closed"|"open"|"half_open", key_hint: "ENV_VAR_NAME"}],
  models: [{
    id: "openai:gpt-6.1-sol",          // "<provider>:<model>", what /run expects
    provider, model, label, tier,
    configured: bool,                   // a server-side key exists for this provider
    available: true|false|null,         // result of live model discovery; null = not checked
    default: bool,                      // pre-selected in the UI
    price_in_per_mtok: number|null, price_out_per_mtok: number|null,
    price_note: string|null,            // e.g. tiered / promotional price caveat
    price_verified_on: "YYYY-MM-DD", source_url,
    context_window: int|null, max_output_tokens: int|null,
    supports: {native_search: bool, structured_output: "native"|"prompted"},
    est_max_cost_usd: number|null       // worst-case for ONE call with current default limits
  }],
  catalog_verified_on: "YYYY-MM-DD", catalog_stale: bool,
  limits: {min_models:1, max_models:4, max_prompt_chars:int, default_max_output_tokens:int,
           max_output_tokens:int, max_cost_per_request_usd:number},
  disclosures: [string]                 // privacy/third-party processing statements to show the user
}
```

## POST /run
Request:
```
{ prompt: string(3..6000),
  models: ["openai:gpt-6.1-sol", ...]  // 1..4 distinct ids
  evidence_mode: "none" | "atlas" | "provider_search",
  include_documents: bool,             // atlas mode: retrieve from user's library
  include_web: bool,                   // atlas mode: Tavily web search
  structured: bool,                    // ask for JSON {answer, claims[{text, sources[]}], abstained}
  max_output_tokens?: int,
  consent_external: bool,              // REQUIRED true when the prompt/evidence leaves Atlas (always true for /run)
  idempotency_key?: string }
```
Response `data`:
```
{ run_id, created_at, prompt, evidence_mode, structured,
  sources: [{id: "D1"|"W1", kind: "document"|"web", title, url|null, domain|null,
             excerpt, retrieved_at, sha256, risk_flags: [string], report_id|null}],
  results: [ModelResult],
  summary: { measurements: [{key, label, winner_id, value, unit}],   // measured facts only
             notes: [string] },                                       // never says "best"
  warnings: [string],
  budget: { est_max_cost_usd, actual_cost_usd|null, cost_complete: bool },
  evaluation: { available: bool, message: string }   // whether measured evaluation evidence exists
}
ModelResult = {
  id, provider, model, label,
  status: "ok"|"failed"|"timeout"|"blocked"|"skipped",
  error: null | {category, message, retryable: bool},
  answer: string|null,
  structured: null | {valid: bool, data: object|null, errors: [string], repaired: bool},
  citations: [{kind: "atlas_source"|"provider_web", source_id|null, url|null, title|null, snippet|null, valid: bool}],
  grounding: null | {mode, cited_labels:[string], invalid_labels:[string],
                     valid_citation_rate: number|null, sentence_coverage: number|null,
                     support_rate: number|null, supported: int, checked: int,
                     method: string, notes: [string]},
  usage: {input_tokens|null, output_tokens|null, reasoning_tokens|null, cached_input_tokens|null,
          total_tokens|null, reported: bool},
  cost: {usd|null, token_cost_usd|null, tool_cost_usd|null, complete: bool,
         basis: "provider_reported"|"catalog_price_estimate"|"unknown", price_verified_on|null},
  latency: {total_ms|null, attempts: [{n, ms, outcome: "ok"|"error"|"timeout", category|null}], retries: int},
  finish_reason: string|null, truncated: bool,
  safety: {flags: [string]},
  request_id
}
```
Status codes: 400 validation / unknown or unavailable model / unconfigured provider,
402 `cost ceiling` (estimated worst case exceeds the per-request ceiling),
409 identical idempotency key still running or a run already in progress for the account,
422 pydantic validation, 429 rate limit or account budget.

## POST /ask
Single answer with an explicit ordered fallback chain (never used by /run).
Request: `{prompt, models: [ids] (1..3, ordered), evidence_mode, include_documents, include_web, structured, max_output_tokens?, consent_external, idempotency_key?}`
Response `data`: `{run_id, result: ModelResult, attempts: [{model_id, status, category|null, ms}], fallback_used: bool, fallback_reason: string|null, sources, warnings, budget}`

## GET /runs · GET /runs/{run_id} · DELETE /runs/{run_id}
Owner-scoped. `/runs` → `{items: [{run_id, created_at, prompt, model_count, evidence_mode}]}`; `/runs/{id}` → same `data` as POST /run.

## GET /evaluations
`data`: `{available: bool, runs: [{eval_id, ran_at, dataset: {name, version, n_cases, sha256},
 models: [{id, n, pass_rate, ci95: [lo, hi], by_category: {cat: {n, pass_rate}}, mean_cost_usd|null, p50_latency_ms|null}],
 ranking_supported: bool, caveats: [string]}], explanation: string}`
`ranking_supported` is true only when confidence intervals separate the models.

## Admin (role = admin)
`GET /admin/metrics` · `GET /admin/health` · `POST /admin/discover` (runs model discovery, no tokens billed).
