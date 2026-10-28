"""Regression test for a real production bug: sentence-transformers (the
package EmbeddingService's default backend depends on) was missing from
requirements.txt, so every embed_text/embed_texts call raised RuntimeError.
Chat and Discover degraded silently rather than crashing — chat answered
with zero retrieved context, and search_similar()'s broad except returned []
— so nothing in the existing test suite caught it: every other test that
touches EmbeddingService correctly monkeypatches embed_text/embed_texts to
avoid a live network call, which means "pytest passes" was never sufficient
evidence that the real embedding backend was actually installed.

This test intentionally does NOT monkeypatch anything — it exercises the
real import path EmbeddingService.__init__ uses, so a future requirements.txt
regression (or an environment sentence-transformers silently fails to
install into) fails loudly here instead of surfacing only as "chat feels
generic" or "Discover finds nothing" days later.
"""
from __future__ import annotations

import pytest


def test_sentence_transformers_embedding_backend_is_installed():
    """Guards against the exact bug this test is named for: importing and
    constructing the real (non-mocked) embedding function chromadb uses
    must not fall back to the "package not installed" path. We don't
    require the model weights to already be downloaded/cached (that needs
    network access to huggingface.co on first use, which CI/sandboxes may
    not have) — only that the sentence-transformers *package* itself,
    which is what was actually missing, is importable and constructible up
    to that point.
    """
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    except ImportError as exc:
        pytest.fail(
            "chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction "
            f"could not be imported: {exc}. This is the exact bug that broke "
            "Ask Atlas chat context and Discover semantic search end to end — "
            "check that sentence-transformers is listed in requirements.txt "
            "and actually installed in this environment."
        )

    try:
        SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    except ValueError as exc:
        # This is exactly chromadb's "the sentence_transformers python
        # package is not installed" error — the package is missing even
        # though the import above succeeded (e.g. partially installed).
        pytest.fail(
            f"sentence-transformers is not properly installed: {exc}. "
            "Run: pip install -r requirements.txt"
        )
    except Exception as exc:
        # Any other failure here (most commonly: no network access to
        # huggingface.co to download the model weights on first use) is a
        # real environment limitation, not the missing-package bug this
        # test guards against — don't fail the suite over it.
        pytest.skip(
            f"sentence-transformers is installed, but model initialization "
            f"failed for an unrelated reason ({type(exc).__name__}: {exc}) — "
            "likely no network access to huggingface.co in this environment."
        )


def test_embedding_service_reports_missing_backend_distinctly(monkeypatch):
    """EmbeddingService.__init__ must distinguish 'package not installed'
    (ImportError) from 'package installed but init failed for another
    reason' (e.g. a blocked model download) — see the comment in
    embedding_service.py. Verifies the branching logic itself, independent
    of whether the real package happens to be installed in this
    environment.
    """
    from backend.memory.embedding_service import EmbeddingService

    def _raise_import_error(*args, **kwargs):
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setattr(
        "chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction",
        _raise_import_error,
    )
    service = EmbeddingService()
    assert service._mode == "none"
    assert service._fn is None
    with pytest.raises(RuntimeError):
        service.embed_text("test")
