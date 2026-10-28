from __future__ import annotations

import threading
import time
from typing import Optional

from backend.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _configured_model_name() -> str:
    """Model name from Settings; never let missing provider keys break embeddings."""
    try:
        from backend.core.config import get_settings

        return get_settings().embedding_model_name or DEFAULT_EMBEDDING_MODEL
    except Exception:
        return DEFAULT_EMBEDDING_MODEL


def _configured_retry_seconds() -> float:
    try:
        from backend.core.config import get_settings

        return float(get_settings().embedding_retry_seconds)
    except Exception:
        return 300.0


class EmbeddingService:
    """
    Free local embeddings using chromadb's built-in sentence-transformers.
    No OpenAI key required.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        self._model_name = model_name or _configured_model_name()
        self._dimension: Optional[int] = None
        # Import lazily so startup doesn't fail if sentence-transformers isn't installed
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            self._fn = SentenceTransformerEmbeddingFunction(model_name=self._model_name)
            self._mode = "sentence_transformers"
        except ImportError as exc:
            # The package itself isn't installed — this is the exact bug that
            # previously broke Ask Atlas (chat) and Discover (semantic search)
            # end to end: both silently degraded (chat answered with zero
            # retrieved context, Discover always returned "no results")
            # instead of failing loudly, because every embed_text/embed_texts
            # call below correctly raises once self._fn is None, but nothing
            # upstream surfaced *why*. Logged distinctly from other init
            # failures (e.g. network/model-download issues) so this class of
            # bug is diagnosable from the log line alone next time.
            self._fn = None
            self._mode = "none"
            logger.error(
                "sentence-transformers package is not installed — embeddings, "
                "Ask Atlas chat context, and Discover semantic search are all "
                "disabled. Run: pip install -r requirements.txt",
                extra={"error": str(exc)},
            )
        except Exception as exc:
            # Package is installed but initialization itself failed — most
            # commonly the model weights couldn't be downloaded from
            # huggingface.co on first use (no network access, or a firewall/
            # proxy blocking that host). Distinguished from the ImportError
            # case above because the fix is different (network access, not
            # `pip install`).
            self._fn = None
            self._mode = "none"
            logger.error(
                "sentence-transformers failed to initialize — embeddings, Ask "
                "Atlas chat context, and Discover semantic search are all "
                "disabled. This is usually a model-download failure (first "
                "use requires network access to huggingface.co to fetch "
                "all-MiniLM-L6-v2), not a missing package.",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )

    @property
    def model_id(self) -> str:
        """Identifier stamped on every stored vector so a model change is detectable."""
        return self._model_name

    @property
    def dimension(self) -> Optional[int]:
        """Vector length once at least one text has been embedded, else ``None``."""
        return self._dimension

    @property
    def mode(self) -> str:
        """'sentence_transformers' once the real backend loaded, else 'none'."""
        return self._mode

    @property
    def is_available(self) -> bool:
        """True once a real embedding backend is loaded and usable."""
        return self._fn is not None

    def embed_text(self, text: str) -> list[float]:
        cleaned = text.strip()
        if not cleaned:
            return []
        if self._fn is None:
            raise RuntimeError(
                "Embedding function unavailable (sentence-transformers failed to "
                "load at startup) — cannot embed text."
            )
        # Let failures propagate. Silently returning [] here previously let
        # a real failure masquerade as a same-length "successful" result,
        # which meant index_report()'s length check never caught it and the
        # actual cause was lost by the time the resulting Chroma upsert
        # failed downstream.
        result = self._fn([cleaned])
        vector = [float(value) for value in result[0]]
        self._dimension = len(vector)
        return vector

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        cleaned = [t.strip() for t in texts if t.strip()]
        if not cleaned:
            return [[] for _ in texts]
        if self._fn is None:
            raise RuntimeError(
                "Embedding function unavailable (sentence-transformers failed to "
                "load at startup) — cannot embed text."
            )
        result = self._fn(cleaned)
        vectors = [[float(value) for value in v] for v in result]
        if vectors:
            self._dimension = len(vectors[0])
        return vectors


_shared_lock = threading.Lock()
_shared_service: Optional[EmbeddingService] = None
_shared_failed_at: float = 0.0


def get_shared_embedding_service() -> EmbeddingService:
    """Process-wide EmbeddingService, loaded once and reused.

    Loading the model costs seconds, so request paths must not each build their
    own. Unlike a plain ``lru_cache`` this *self-heals*: if the backend failed
    to load (e.g. a transient model-download failure) it is retried after
    ``EMBEDDING_RETRY_SECONDS`` instead of staying broken until a restart.
    """
    global _shared_service, _shared_failed_at
    with _shared_lock:
        now = time.monotonic()
        if _shared_service is not None:
            if _shared_service.is_available or (now - _shared_failed_at) < _configured_retry_seconds():
                return _shared_service
        service = EmbeddingService()
        if not service.is_available:
            _shared_failed_at = now
        _shared_service = service
        return service


def reset_shared_embedding_service() -> None:
    """Drop the cached instance (tests / explicit reload)."""
    global _shared_service, _shared_failed_at
    with _shared_lock:
        _shared_service = None
        _shared_failed_at = 0.0


def get_status_embedding_service() -> EmbeddingService:
    """Cheap status/health accessor (e.g. GET /health). Shares the process-wide
    instance, so polling never reloads the model."""
    return get_shared_embedding_service()
