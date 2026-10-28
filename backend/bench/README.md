# AtlasBench (M8)

Run the **same research task over the same frozen evidence** through different models and
agent settings, score the results deterministically, persist them per user, and compare.

## Why the results are trustworthy
* **Frozen world.** A task carries its own evidence corpus. Retrieval is the M5 hybrid retriever
  (lexical stage) over that corpus; "scraping" returns the stored text and never opens a socket.
  Built-in corpora are fictional so a model cannot answer from memory.
* **Real pipeline.** Runs go through the M4 tool registry (`search_web`, `scrape_source`,
  `verify_claim`, `request_more_evidence`) and the real `VerificationAgent`; only the chat model
  differs per configuration. Corpus text is untrusted data (M4 wrapper) — one built-in task carries a
  prompt-injection document to prove it.
* **No LLM judge.** Every metric is a documented rule over (gold, output) in `scoring.py`; scoring is a
  pure function (tested for determinism). Metrics that do not apply are `null`, never 0.
* **Reproducible by construction.** Tasks and configs are content-hashed; a run stores snapshots of
  both plus a fingerprint (`evaluator + config hash + task hashes`). `POST /runs/{id}/reproduce`
  creates a new run over the exact snapshots. Comparison aligns on `(task_id, task_hash)` and refuses to
  mix task versions. Live-model nondeterminism is not hidden: identical-fingerprint runs can still differ.

## Measures (weights of the heuristic composite)
claim correctness .25 · citation accuracy .20 · evidence grounding .20 · evidence coverage .15 ·
contradiction handling .10 · tool/trajectory quality .10. Latency, tokens and estimated cost are
reported separately (tokens are provider-reported or `null`; cost is `null` for unpriced models).

## Operations
* Run states `pending → running → completed | failed`, conditional updates, lease + heartbeat;
  cases persist one by one so an interrupted run resumes; failed cases retry (max 2), runs max 3 attempts.
* One executing run per account; `execute` rate-limited (6 / 5 min); idempotent for completed runs.
* Models must be on an allowlist (`MODEL_CATALOG` + the app's default model): no arbitrary model strings.

## Known limits
* Derived tasks (from Knowledge Map claims / M7 challenges) have **derived**, not human-reviewed, gold — marked in API and UI.
* Citation relevance/grounding use lexical overlap (thresholds in `scoring.py`); they can miss paraphrased quotes.
* Prices in `MODEL_CATALOG` are estimates; verify against the provider before relying on cost numbers.

## M10 output arbitration

`POST /api/atlasbench/arbitrations` accepts a task ID and 2–4 run IDs. At least two must contain
completed cases for that same frozen task, and those eligible outputs must represent distinct
models. The endpoint reuses AtlasBench's stored correctness, grounding, citation, coverage,
and contradiction scores. It selects one preserved output; it does not call another model or
synthesize unsupported claims. The full candidate snapshots, model/provider identity, config and
run hashes, evaluator version, scores, output, token/cost and latency data, selected run, and
rationale are persisted in a user-scoped arbitration record.
Only completed outputs count toward the minimum of two models; failed outputs cannot establish
model diversity or be selected. Eligible outputs must share the task hash (which covers the frozen
task/evidence), any supplied evidence hash, and evaluator version. Metrics that are null/not
applicable or missing for any eligible candidate are excluded for every candidate. Selection is
rejected unless at least three shared metrics cover at least 50% of the documented composite
weight; the remaining weights are renormalized identically across candidates. Insufficient evidence
returns an explicit client error and no winner.
If the two best weighted scores are equal at AtlasBench's recorded precision, the tie is also
reported as insufficient evidence rather than broken by a non-evidence model-name ordering.
* Real-model runs were not executed in development (no API key); all tests use a scripted model behind the real pipeline.

### Optional arbitration quality floor

Deployments may set `ATLASBENCH_MIN_ARBITRATION_QUALITY` to a finite value from `0.0` to `1.0`. It is a floor on the measured, renormalized composite score calculated from shared AtlasBench evaluator metrics; it is **not** a confidence probability. The default `0.0` preserves the prior selection behavior. If the leading candidate is below the configured floor, AtlasBench persists an `abstained` arbitration with its measured scores and rationale and returns no selected model/output. Evidence insufficiency and tied scores continue to reject selection rather than choosing a winner.
