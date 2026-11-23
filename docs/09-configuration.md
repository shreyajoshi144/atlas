# Configuration reference

Atlas reads settings from environment variables, and from a `.env` file in development (copy [`.env.example`](../.env.example)). Environment variables always win over `.env`. A setting's name is shown exactly as you set it.

## Required

| Variable | Notes |
|---|---|
| `GROQ_API_KEY` | Default chat model provider. The app will not start without it. |
| `TAVILY_API_KEY` | Web search. The app will not start without it. |

## Production (`APP_ENV=production`)

Startup is refused, with a message naming the setting, unless all of these are set. The Docker image sets `APP_ENV=production`.

| Variable | Rule |
|---|---|
| `SECRET_KEY` | Signs login tokens. At least 32 characters, not a known placeholder, not low-entropy. Generate one with `python -c "import secrets;print(secrets.token_urlsafe(48))"`. |
| `CORS_ALLOWED_ORIGINS` | Comma-separated explicit origins. Wildcards are rejected. On single-origin hosting the browser never makes a cross-origin call, but the setting is still required; use the app's own URL. |
| `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` | Per-user monthly spend cap in USD. |

## Spend and usage limits

| Variable | Default | Notes |
|---|---|---|
| `DEFAULT_DAILY_LLM_CALL_LIMIT` | `1000` | Per user per day. |
| `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` | unset | Per user per month. Required in production. |
| `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD` | unset | Ceiling across all accounts. Recommended for any public deployment: per-user caps multiply with every sign-up. While it is set, calls whose cost cannot be estimated are refused. |

Limits are enforced by an atomic reservation before each call and return `429`. Cost figures are estimates from catalog prices, not provider invoices. Admins can override a user's limits through `PUT /api/admin/users/{user_id}/limits`.

## Web and security

| Variable | Default | Notes |
|---|---|---|
| `FRAME_ANCESTORS` | `'none'` | Who may embed the frontend pages in an iframe (CSP `frame-ancestors`). Accepts `'none'`, `'self'`, or space-separated `https://` origins; anything else stops startup. API responses are never embeddable. `deploy/start_space.sh` sets `https://huggingface.co https://*.hf.space` so a Hugging Face Space page can show the app. |
| `FRONTEND_BASE_URL` | `http://localhost:7860` | Used to build password-reset links. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | Login token lifetime (7 days). |
| `PASSWORD_RESET_EXPIRE_MINUTES` | `30` | |
| `MAX_REQUEST_BODY_BYTES` | `2097152` | Larger bodies get `413`. |
| `FORWARDED_ALLOW_IPS` | `*` (start script) | Proxy IPs whose `X-Forwarded-For` is trusted. On a host with a known proxy, set it explicitly so clients cannot spoof their IP past the rate limiter. |

## Models and search

| Variable | Default |
|---|---|
| `OPENAI_MODEL` | `openai/gpt-oss-120b` (served through Groq) |
| `OPENAI_TEMPERATURE` / `OPENAI_MAX_TOKENS` | `0.1` / `8192` |
| `WRITER_MAX_OUTPUT_TOKENS` / `WRITER_RECOVERY_MAX_OUTPUT_TOKENS` | `8192` / `4096` |
| `WRITER_REQUEST_TIMEOUT_SECONDS` | `60` |
| `TAVILY_MAX_RESULTS` | `20` |
| `DEFAULT_TOP_K_URLS` | `4` |
| `REQUEST_TIMEOUT_SECONDS` / `SCRAPE_TIMEOUT_SECONDS` | `20` / `12` |
| `SCRAPE_MAX_RETRIES` / `SCRAPE_CONCURRENCY` | `2` / `5` |
| `MAX_CONTENT_CHARS_PER_SOURCE` | `2200` |
| `MAX_EVIDENCE_CHARS_PER_LLM_CALL` | `24000` |

## Retrieval and embeddings

Embeddings run locally with a sentence-transformers model, so `OPENAI_API_KEY` is optional (it only enables OpenAI in Compare).

| Variable | Default |
|---|---|
| `EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` |
| `RETRIEVAL_RRF_K` | `60` |
| `RETRIEVAL_LEXICAL_WEIGHT` / `RETRIEVAL_DENSE_WEIGHT` | `1.0` / `1.0` |
| `RETRIEVAL_CANDIDATE_DEPTH` | `50` |
| `RETRIEVAL_MAX_SCAN_CHUNKS` | `5000` |
| `EMBEDDING_RETRY_SECONDS` | `300` (a failed embedding backend is retried no more often than this) |

## Multi-provider Compare

A provider is enabled when its key is set to a real value (placeholders such as `dummy` count as not set).

| Variable | Notes |
|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `XAI_API_KEY` | Server-side only; never sent to the browser. |
| `COMPARE_MAX_COST_PER_REQUEST_USD` | Default `0.50`. A run whose worst-case estimate exceeds it is refused before any call. |
| `COMPARE_MAX_MODELS`, `COMPARE_MAX_PROMPT_CHARS`, `COMPARE_MAX_EVIDENCE_CHARS`, `COMPARE_MAX_OUTPUT_TOKENS`, `COMPARE_DEFAULT_MAX_OUTPUT_TOKENS` | Request size limits. |
| `COMPARE_RETENTION_DAYS` | Default `90`. Saved comparison runs are deleted after this. |
| `PROVIDER_TIMEOUT_SECONDS` / `PROVIDER_DEADLINE_SECONDS` / `PROVIDER_MAX_RETRIES` | `90` / `170` / `2`: per attempt, all attempts for one call, and retries. |
| `PROVIDER_BACKOFF_BASE_SECONDS`, `PROVIDER_BACKOFF_CAP_SECONDS`, `PROVIDER_RETRY_AFTER_CAP_SECONDS` | Retry timing. |
| `PROVIDER_BREAKER_THRESHOLD`, `PROVIDER_BREAKER_COOLDOWN_SECONDS` | Circuit breaker per provider. |
| `PROVIDER_CONCURRENCY`, `PROVIDER_GLOBAL_CONCURRENCY` | Parallel call limits. |
| `PROVIDER_DISCOVERY_TTL_SECONDS` | How long discovered model lists are cached. |
| `PROVIDER_ALLOW_CUSTOM_BASE_URL`, `PROVIDER_ALLOW_INSECURE_HTTP` | Off unless set to exactly `1`. They let a provider use a gateway base URL or plain HTTP. Keep them off except when testing against a local fake provider. |
| `MODEL_CATALOG_PATH`, `COMPARE_EVAL_DIR` | Custom catalog file and evaluation result directory. |

## Storage and logging

| Variable | Default | Notes |
|---|---|---|
| `ATLAS_DATA_DIR` | `<repo>/data` | Root for the SQLite database, the Chroma vector store and logs. Point it at a durable volume in production. |
| `SQLITE_PATH` / `CHROMA_PATH` / `ATLAS_LOG_DIR` | under `ATLAS_DATA_DIR` | Individual overrides. |
| `LOG_LEVEL` | `INFO` | Logs are JSON lines with request ids, with secrets redacted. |
| `APP_ENV` | `development` | `production` turns on the startup validation above and HSTS. |
| `DEBUG` | `true` | Only adds traceback detail to the **server log**, and only when `APP_ENV=development`. Never changes responses. |
| `SENTRY_DSN` | unset | Optional error tracking. PII and request bodies are not sent. |
| `ATLASBENCH_MIN_ARBITRATION_QUALITY` | `0.0` | Minimum quality score a run needs before AtlasBench arbitration will compare it (0 to 1). |

## Demo account (Space start script only)

| Variable | Notes |
|---|---|
| `DEMO_EMAIL`, `DEMO_PASSWORD` | When both are set, `deploy/seed_demo.py` creates this account at start-up if it does not exist. `DEMO_NAME` sets its display name. |
