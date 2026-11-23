#!/bin/sh
set -e
# FORWARDED_ALLOW_IPS: comma-separated proxy IPs whose X-Forwarded-For is trusted.
# Defaults to "*" because the platform proxy address is not fixed on Spaces; on a host
# with a known proxy, set it explicitly so clients cannot spoof their IP past the rate limiter.
# The Space page embeds the app in an iframe from huggingface.co; allow only those origins.
export FRAME_ANCESTORS="${FRAME_ANCESTORS:-https://huggingface.co https://*.hf.space}"
python deploy/seed_demo.py
exec python -m uvicorn deploy.space_app:app --host 0.0.0.0 --port "${PORT:-7860}" \
  --workers 1 --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"
