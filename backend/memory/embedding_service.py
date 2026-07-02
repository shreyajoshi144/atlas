from __future__ import annotations

from backend.core.logging import get_logger

logger = get_logger(__name__)


class EmbeddingService:
    """
    Free local embeddings using chromadb's built-in sentence-transformers.
    No OpenAI key required.
    """

    def __init__(self) -> None:
        # Import lazily so startup doesn't fail if sentence-transformers isn't installed
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            self._fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            self._mode = "sentence_transformers"
        except Exception:
            self._fn = None
            self._mode = "none"
            logger.warning("sentence-transformers not available — embeddings disabled")

    def embed_text(self, text: str) -> list[float]:
        cleaned = text.strip()
        if not cleaned or self._fn is None:
            return []
        try:
            result = self._fn([cleaned])
            return list(result[0])
        except Exception as e:
            logger.warning("embed_text failed", extra={"error": str(e)})
            return []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        cleaned = [t.strip() for t in texts if t.strip()]
        if not cleaned or self._fn is None:
            return [[] for _ in texts]
        try:
            result = self._fn(cleaned)
            return [list(v) for v in result]
        except Exception as e:
            logger.warning("embed_texts failed", extra={"error": str(e)})
            return [[] for _ in cleaned]
