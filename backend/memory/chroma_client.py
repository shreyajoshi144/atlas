from __future__ import annotations

from chromadb import PersistentClient
from chromadb.api.models.Collection import Collection

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


def _get_embedding_function():
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        return SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    except Exception:
        return None


class ChromaMemoryClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = PersistentClient(path=settings.chroma_path)
        self.collection_name = "atlas_report_chunks"
        self._ef = _get_embedding_function()

    def get_collection(self) -> Collection:
        kwargs = dict(
            name=self.collection_name,
            metadata={"description": "Atlas AI chunked report memory"},
        )
        if self._ef is not None:
            kwargs["embedding_function"] = self._ef

        collection = self.client.get_or_create_collection(**kwargs)
        logger.info(
            "Chroma collection ready",
            extra={"collection_name": self.collection_name, "count": collection.count()},
        )
        return collection
