#!/usr/bin/env python3
"""
Atlas AI — one-command launcher.
Starts the FastAPI backend (uvicorn) and a static file server for index.html,
waits for the backend to report healthy, then opens the app in your browser.

Usage:
    python start.py
Stop with Ctrl+C — both servers shut down together.
"""
from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent
BACKEND_PORT = 8000
FRONTEND_PORT = 5500

if not (ROOT / ".env").exists():
    print("⚠  No .env file found next to start.py.")
    print("   Copy .env.example to .env and fill in GROQ_API_KEY / TAVILY_API_KEY first.")
    sys.exit(1)

if not (ROOT / "index.html").exists():
    print("⚠  index.html not found next to start.py.")
    sys.exit(1)

print("→ Starting backend (uvicorn) on port", BACKEND_PORT, "...")
backend = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "main:app", "--port", str(BACKEND_PORT)],
    cwd=str(ROOT),
)

print("→ Starting frontend (static server) on port", FRONTEND_PORT, "...")
frontend = subprocess.Popen(
    [sys.executable, "-m", "http.server", str(FRONTEND_PORT)],
    cwd=str(ROOT),
)

print("→ Waiting for backend to become healthy...")
health_url = f"http://localhost:{BACKEND_PORT}/health"
for _ in range(30):
    try:
        with urlopen(health_url, timeout=1) as r:
            if r.status == 200:
                print("✓ Backend is up.")
                break
    except URLError:
        pass
    time.sleep(1)
else:
    print("⚠  Backend did not report healthy in time — check its terminal output above.")

app_url = f"http://localhost:{FRONTEND_PORT}/index.html"
print(f"→ Opening {app_url}")
webbrowser.open(app_url)

print("\nAtlas AI is running.")
print(f"  Backend:  http://localhost:{BACKEND_PORT}  (docs at /docs)")
print(f"  Frontend: {app_url}")
print("Press Ctrl+C to stop both.\n")

try:
    backend.wait()
except KeyboardInterrupt:
    pass
finally:
    for p in (backend, frontend):
        if p.poll() is None:
            p.terminate()
    for p in (backend, frontend):
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    print("Stopped.")