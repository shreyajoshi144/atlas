#!/bin/sh
set -e
python deploy/seed_demo.py
exec python -m uvicorn deploy.space_app:app --host 0.0.0.0 --port "${PORT:-7860}" \
  --workers 1 --proxy-headers --forwarded-allow-ips="*"
