#!/usr/bin/env python3
"""
Atlas AI — one-command launcher.
Starts the FastAPI backend (uvicorn) and a static file server for the
frontend/ folder (index.html landing + auth.html + app.html + css/ + js/ + assets/),
waits for the backend to report healthy, then opens the landing page
in your browser.

Usage:
    python start.py
Stop with Ctrl+C — both servers shut down together.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
PORT = int(os.environ.get("PORT", 7860))  # API + frontend share one origin, same as Docker / Spaces

if not (ROOT / ".env").exists():
    print("⚠  No .env file found next to start.py.")
    print("   Copy .env.example to .env and fill in GROQ_API_KEY / TAVILY_API_KEY first")
    print("   (plus OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY / XAI_API_KEY for Compare).")
    sys.exit(1)

if not (FRONTEND_DIR / "index.html").exists():
    print("⚠  frontend/index.html (landing page) not found next to start.py.")
    sys.exit(1)

if not (FRONTEND_DIR / "app.html").exists():
    print("⚠  frontend/app.html (workspace) not found next to start.py.")
    sys.exit(1)


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is already listening on host:port. subprocess.Popen()
    below can't tell us this — it returns immediately whether or not the
    child actually managed to bind, so a stale process left over from a
    start.py run that didn't shut down cleanly (closed terminal tab, force
    quit, etc. — anything that skips the try/finally cleanup at the bottom
    of this file) would otherwise go unnoticed: the new subprocesses would
    silently fail to bind while the health check below happily talks to the
    OLD orphaned backend instead, making everything look fine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
        return False


for _port, _name in ((PORT, "atlas"),):
    if _port_in_use(_port):
        print(f"⚠  Port {_port} ({_name}) is already in use.")
        print("   This usually means a previous `python start.py` is still running")
        print("   (e.g. its terminal tab was closed instead of pressing Ctrl+C).")
        print("   Find and stop it, then try again:")
        print(f"     lsof -nP -iTCP:{_port} -sTCP:LISTEN")
        print(f"     lsof -ti:{PORT} | xargs kill -9")
        sys.exit(1)

print("→ Starting Atlas (API + frontend) on port", PORT, "...")
backend = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "deploy.space_app:app", "--port", str(PORT)],
    cwd=str(ROOT),
)

print("→ Waiting for backend to become healthy...")
health_url = f"http://localhost:{PORT}/health"
for _ in range(30):
    try:
        with urlopen(health_url, timeout=1) as r:
            if r.status == 200:
                print("✓ Backend is up.")
                break
    except OSError:
        # Covers both a plain "nobody's listening yet" (urllib.error.URLError,
        # e.g. connection refused while uvicorn is still importing/starting)
        # and a slow-to-respond backend (socket.timeout / TimeoutError) — both
        # are OSError subclasses. The previous `except URLError:` here missed
        # TimeoutError, so a backend that took just over `timeout=1` seconds
        # to answer (e.g. the first request that loads the embedding model)
        # crashed this whole launcher instead of just retrying like it
        # should for every other kind of "not ready yet" during this loop.
        pass
    time.sleep(1)
else:
    print("⚠  Backend did not report healthy in time — check its terminal output above.")

landing_url = f"http://localhost:{PORT}/index.html"
app_url = f"http://localhost:{PORT}/app.html"
print(f"→ Opening {landing_url}")
webbrowser.open(landing_url)

print("\nAtlas AI is running.")
print(f"  API:      http://localhost:{PORT}/api  (docs at /docs)")
print(f"  Landing:  {landing_url}")
print(f"  App:      {app_url}")
print("Press Ctrl+C to stop.\n")

try:
    backend.wait()
except KeyboardInterrupt:
    pass
finally:
    if backend.poll() is None:
        backend.terminate()
    try:
        backend.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend.kill()
    print("Stopped.")