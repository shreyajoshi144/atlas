# Deployment guide

Atlas ships as one container: the FastAPI app and the static frontend share one origin on port 7860. This guide covers running that image locally and deploying it to **Render** (where the live demo runs) or to a Hugging Face Docker Space. The live demo is https://atlas-cite-check.onrender.com. Day-two operations (health, limits, admin) are in the [runbook](06-runbook.md); every setting is in the [configuration reference](09-configuration.md).

## What the image does

| | |
|---|---|
| Base | `python:3.13-slim`, runs as uid 1000. CPU-only PyTorch is installed for the optional sentence-transformers path, but the container's default embedding path is ONNX. |
| Entry point | `deploy/start_space.sh`: seeds the optional demo account, then `uvicorn deploy.space_app:app` on port 7860 with one worker |
| Frontend | `deploy/space_app.py` mounts `frontend/` after all API routes. HTML, JS, CSS and JSON are served `no-cache` (revalidated with an ETag), so a new deploy shows up on a normal reload. |
| Embeddings | The ONNX build of `all-MiniLM-L6-v2` is downloaded at build time, so start-up needs no network. |
| Small-host mode | The image sets `ATLAS_EMBEDDING_BACKEND=onnx`, `ATLAS_DENSE_RANKING=0`, `ATLAS_MAX_TOP_K=6` and `ATLAS_LIGHT_MODE=1`. See [Small-host mode](#small-host-mode-512-mb). Override them on a larger host. |
| Health | `GET /ready` (database answers) and `GET /health`. The image's `HEALTHCHECK` uses `/ready`. |
| Architectures | Built and run on `linux/arm64` and `linux/amd64`. Render and Hugging Face run amd64. |
| Embedding in an iframe | `deploy/start_space.sh` allows only `https://huggingface.co` and `https://*.hf.space` (`FRAME_ANCESTORS`). Set `FRAME_ANCESTORS='none'` on Render, which does not embed the app. |
| API docs | `/docs`, `/redoc` and `/openapi.json` are off when `APP_ENV=production`. Set `ENABLE_API_DOCS=true` to turn them on. |

## Run it locally with Docker

```sh
docker build -t atlas .
docker run --rm -p 7860:7860 --env-file .env -v atlas-data:/home/user/app/data atlas
```

Or with Compose, which builds the image, reads `.env` and keeps data in a named volume:

```sh
docker compose up --build
```

The first build takes several minutes (PyTorch and the embedding model); later builds reuse the cached layers. In `.env`, `APP_ENV=production` additionally requires `SECRET_KEY`, `CORS_ALLOWED_ORIGINS` and `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` (see the [configuration reference](09-configuration.md)). Then open http://localhost:7860.

Without `-v`, the database is discarded with the container.

## Deploy to Render (free tier)

This is how the live demo is deployed. Render builds the repository's `Dockerfile` and redeploys on every push to the watched branch.

1. **Push the repository to GitHub.** Render watches one branch (this project uses `main`).
2. **Create the service.** In the Render dashboard choose *New*, *Web Service*, connect the repository, and set **Language: Docker**, **Branch: main**, **Instance type: Free**.
3. **Choose a unique name.** The name becomes the URL (`https://<name>.onrender.com`). If the name is taken, Render silently appends a random suffix, and the URL of an existing service cannot be changed. Pick a distinctive name before the first deploy.
4. **Set environment variables** (*Environment* tab):

   | Name | Value |
   |---|---|
   | `SECRET_KEY` | 32+ random characters (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
   | `GROQ_API_KEY`, `TAVILY_API_KEY` | your keys (both are required to start) |
   | `GEMINI_API_KEY` | optional; enables the two Gemini models in the AI Playground (Google AI Studio has a free tier) |
   | `APP_ENV` | `production` |
   | `CORS_ALLOWED_ORIGINS` | `https://<name>.onrender.com` |
   | `DEFAULT_MONTHLY_LLM_SPEND_CAP_USD` | for example `5` |
   | `GLOBAL_MONTHLY_LLM_SPEND_CAP_USD` | for example `20` |
   | `FRAME_ANCESTORS` | `'none'` |
   | `DEMO_EMAIL`, `DEMO_PASSWORD` | a demo login that is re-created at every start (see [Storage](#storage)) |

5. **Set the health check path** to `/ready`.
6. **Deploy** and watch the *Logs* tab until the service is *Live*. Then verify:

   ```sh
   curl https://<name>.onrender.com/ready      # {"status":"ready"}
   curl https://<name>.onrender.com/health     # "status":"ok", "embeddings_mode":"onnx"
   ```

7. **Keep it awake (optional).** A free instance sleeps after about 15 minutes without traffic, and the next request takes about a minute. A free uptime monitor that requests `/ready` every 5 minutes avoids the sleeps, which also avoids the data loss that comes with a restart.

Update: push to `main`. Roll back: *Events*, then redeploy an earlier deploy.

## Deploy to a Hugging Face Space (alternative)

Docker Spaces require a paid Hugging Face plan (the free plan offers only static Spaces), so this path was documented and the image checked on both architectures, but it was **not** used for the live demo.

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
   curl https://<user>-<space>.hf.space/health     # "status":"ok", "embeddings_mode":"onnx"
   ```

   Open the Space page on huggingface.co as well, to confirm the app displays inside the embedded frame, then sign up or sign in with the demo account.

## Storage

The database (SQLite) and vector store (Chroma) live under `ATLAS_DATA_DIR`. On free tiers (Render and Hugging Face) the disk is wiped on every restart or redeploy, so accounts and reports created there are lost; the demo account set by `DEMO_EMAIL` and `DEMO_PASSWORD` is re-created at every start. On Render a restart happens at each deploy and when the instance was killed for running out of memory.

To keep data on Render, use a paid instance with a persistent disk mounted at the data directory. On Hugging Face, attach **persistent storage** to the Space (a paid add-on, mounted at `/data`) and set the variable `ATLAS_DATA_DIR=/data`. Self-hosted: mount a volume at `/home/user/app/data` (as the commands above do) or set `ATLAS_DATA_DIR`.

## Small-host mode (512 MB)

Render's free instance has 512 MB of memory and a fraction of a CPU. Before this mode, one research run peaked at about 870 MB locally and the hosted app was killed mid-run, so research failed and all accounts disappeared with the restart. The image now sets four variables:

| Variable | Effect |
|---|---|
| `ATLAS_EMBEDDING_BACKEND=onnx` | Uses the ONNX build of the same embedding model, without PyTorch |
| `ATLAS_DENSE_RANKING=0` | Ranks sources with keyword and credibility signals only (embedding the result pool took about 25 s per search on the free CPU) |
| `ATLAS_MAX_TOP_K=6` | At most 6 sources per run, so the search pool is at most 12 results |
| `ATLAS_LIGHT_MODE=1` | Skips the optional executive brief and vector indexing; the report is still saved to the database and appears in the Library |

Measured locally: research runs completed in about 40 to 65 s, and peak memory was about 210 MB (measured with the ranking and source-cap settings, before light mode was added). Trade-offs: source ranking is less semantic, and because new reports are not indexed, **Ask Atlas reads only the open case or the three newest saved reports on the small host**, not a search over all of them. On a larger host, unset these variables to get the full behaviour.

## Constraints to know about

- **One worker.** Rate limits and request metrics are held in process memory. The start script runs a single uvicorn worker; do not scale out or add workers without moving these to a shared store.
- **Proxy headers.** `FORWARDED_ALLOW_IPS` defaults to `*` because the platform proxy address is not fixed. On a host with a known proxy, set it to that address so clients cannot spoof their IP past the rate limiter.
- **Request time limit.** A hosted proxy can cut a request that runs for about 100 seconds or more. Small-host mode keeps a research run well inside that on a fast CPU, but the free Render CPU is slower, so a run can still approach it.
- **No email provider.** Password reset cannot send mail, and in production the forgot-password response returns no token, so a reset link is never delivered. Plan for admin-assisted resets on a public deployment.
- **Costs are bounded, not guaranteed.** The spend caps use catalog-price estimates and reservations. Treat them as strong guard rails, not as invoices.

## Update and roll back

Hugging Face update: commit to the GitHub repository, rebuild the Space branch the same way (step 2) and push it; the Space rebuilds automatically. Roll back: push the previous Space commit (`git push space <old-commit>:main --force`). Data under `ATLAS_DATA_DIR` is not touched by a rebuild when persistent storage is attached.

## Pre-deploy checklist

- [ ] `python -m pytest -q` and the other suites in the [testing guide](04-testing-and-results.md) pass
- [ ] `docker build` succeeds and `/ready` returns 200 from the running container
- [ ] After the first deploy: sign in with the demo account, run one research case, sign out and sign in again
- [ ] No `.env` or database file is tracked (`git ls-files | grep -E "\.env$|\.db$"` prints nothing)
- [ ] API keys were rotated if `.env` was ever shared
- [ ] Spend caps are set, and `SECRET_KEY` is unique to this deployment
