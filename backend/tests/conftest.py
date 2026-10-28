"""Shared test-environment guards.

The suite must be hermetic: it may never reach the network, and in particular
must never try to download embedding-model weights from huggingface.co. Real
dense-embedding behaviour is exercised through injected embedders (see
``backend/tests/test_m5_hybrid_retrieval.py``); a separate, opt-in integration
test covers the real model when it is available locally.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")
# Application import/startup creates configured runtime directories. Keep the
# suite's default database, Chroma, and logs under the system temp directory,
# never alongside the checked-out source tree.
os.environ.setdefault(
    "ATLAS_DATA_DIR",
    os.path.join(tempfile.gettempdir(), f"atlas-pytest-{os.getpid()}"),
)
# Fail fast (instead of retrying a blocked download) if anything asks for the
# real sentence-transformers model while the suite is running.
# Opt in to downloading real weights with ATLAS_REAL_EMBEDDING_TESTS=1
# (see backend/tests/test_m5_real_embedding_integration.py).
if os.environ.get("ATLAS_REAL_EMBEDDING_TESTS") != "1":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
