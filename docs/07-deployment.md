# Deployment guide

Atlas ships as one container: the FastAPI app and the static frontend share one origin on port 7860. This guide covers running that image locally and deploying it to a Hugging Face Docker Space. Day-two operations (health, limits, admin) are in the [runbook](06-runbook.md); every setting is in the [configuration reference](09-configuration.md).

## What the image does

| | |
|---|---|
| Base | `python:3.13-slim`, CPU-only PyTorch, runs as uid 1000 |
| Entry point | `deploy/start_space.sh`: seeds the optional demo account, then `uvicorn deploy.space_app:app` on port 7860 with one worker |
| Frontend | `deploy/space_app.py` mounts `frontend/` after all API routes. HTML, JS, CSS and JSON are served `no-cache` (revalidated with an ETag), so a new deploy shows up on a normal reload. |
| Embeddings | The `all-MiniLM-L6-v2` model is downloaded at build time, so start-up needs no network. |
| Health | `GET /ready` (database answers) and `GET /health`. The image's `HEALTHCHECK` uses `/ready`. |
| Architectures | Built and run on `linux/arm64` and `linux/amd64`. Hugging Face runs amd64. |
| Embedding in an iframe | Allowed only from `https://huggingface.co` and `https://*.hf.space` (`FRAME_ANCESTORS`). Set it differently, or to `'none'`, on any other host. |

## Run it locally with Docker

```sh
docker build -t atlas .
docker run --rm -p 7860:7860 --env-file .env -v atlas-data:/home/user/app/data atlas
```

Or with Compose, which builds the image, reads `.env` and keeps data in a named volume:

```sh
docker compose up --build
```

The first build takes roughly 15 minutes (PyTorch and the embedding model); later builds reuse the cached layers. In `.env`, `APP_ENV=production` additionally requires `SECRET_KEY`, `CORS_ALLOWED_ORIGINS` and `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` (see the [configuration reference](09-configuration.md)). Then open http://localhost:7860.

Without `-v`, the database is discarded with the container.

## Deploy to a Hugging Face Space

1. **Create the Space.** On huggingface.co choose *New Space*, SDK **Docker**, and name it (for example `atlas`). Your Space URL will be `https://<user>-<space>.hf.space`.
2. **Add the Space's front matter.** A Space needs a `README.md` with YAML front matter at the repository root. [`deploy/space/README.md`](../deploy/space/README.md) is that file (`sdk: docker`, `app_port: 7860`). The GitHub repository keeps its own README, so the Space gets a separate branch:

   ```sh
   git remote add space https://huggingface.co/spaces/<user>/<space>
   git checkout -b space-deploy
   cp deploy/space/README.md README.md
   git add README.md && git commit -m "Space: add front matter"
   git push space space-deploy:main
   git checkout -
   ```

   Git asks for your Hugging Face username and a **write** access token (Settings, Access Tokens). Use `--force` on the push only if the Space already has unrelated history that you want to replace.
3. **Set secrets and variables** in the Space's *Settings*. Put keys under **Secrets**, plain values under **Variables**:

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `SECRET_KEY` | 32+ random characters (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
   | Secret | `GROQ_API_KEY`, `TAVILY_API_KEY` | your keys |
   | Secret | `DEMO_PASSWORD` | password for the optional demo account |
   | Variable | `APP_ENV` | `production` |
   | Variable | `CORS_ALLOWED_ORIGINS` | `https://<user>-<space>.hf.space` |
   | Variable | `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` | for example `5` |
   | Variable | `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD` | for example `20` |
   | Variable | `DEMO_EMAIL` | the demo account's email (with `DEMO_PASSWORD`, optional) |

   Optional provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`) enable more models in Compare.
4. **Wait for the build** (the Space page shows the log), then verify:

   ```sh
   curl https://<user>-<space>.hf.space/ready      # {"status":"ready"}
   curl https://<user>-<space>.hf.space/health     # "status":"ok", "embeddings_mode":"sentence_transformers"
   ```

   Open the Space page on huggingface.co as well, to confirm the app displays inside the embedded frame, then sign up or sign in with the demo account.

## Storage

The database (SQLite) and vector store (Chroma) live under `ATLAS_DATA_DIR`. On the free Space tier the disk is wiped on every restart, so accounts and reports created there are lost; the demo account is re-created on start.

To keep data, attach Hugging Face **persistent storage** to the Space (a paid add-on, mounted at `/data`) and set the variable `ATLAS_DATA_DIR=/data`. Self-hosted: mount a volume at `/home/user/app/data` (as the commands above do) or set `ATLAS_DATA_DIR`.

## Constraints to know about

- **One worker.** Rate limits and request metrics are held in process memory. The start script runs a single uvicorn worker; do not scale out or add workers without moving these to a shared store.
- **Proxy headers.** `FORWARDED_ALLOW_IPS` defaults to `*` because the Space proxy address is not fixed. On a host with a known proxy, set it to that address so clients cannot spoof their IP past the rate limiter.
- **No email provider.** Password reset cannot send mail, and in production the forgot-password response returns no token, so a reset link is never delivered. Plan for admin-assisted resets on a public deployment.
- **Costs are bounded, not guaranteed.** The spend caps use catalog-price estimates and reservations. Treat them as strong guard rails, not as invoices.

## Update and roll back

Update: commit to the GitHub repository, rebuild the Space branch the same way (step 2) and push it; the Space rebuilds automatically. Roll back: push the previous Space commit (`git push space <old-commit>:main --force`). Data under `ATLAS_DATA_DIR` is not touched by a rebuild when persistent storage is attached.

## Pre-deploy checklist

- [ ] `python -m pytest -q` and the other suites in the [testing guide](04-testing-and-results.md) pass
- [ ] `docker build` succeeds and `/ready` returns 200 from the running container
- [ ] No `.env` or database file is tracked (`git ls-files | grep -E "\.env$|\.db$"` prints nothing)
- [ ] API keys were rotated if `.env` was ever shared
- [ ] Spend caps are set, and `SECRET_KEY` is unique to this deployment
