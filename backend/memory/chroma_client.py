from __future__ import annotations

from chromadb import PersistentClient
from chromadb.config import Settings as ChromaSettings
from chromadb.api.models.Collection import Collection
from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


class NoOpProductTelemetry(ProductTelemetryClient):
    """Chroma product-telemetry component that never sends anything.

    ``anonymized_telemetry=False`` alone is not enough: chromadb's bundled
    Posthog client still calls ``posthog.capture(user_id, event, props)``
    positionally (it only flips ``posthog.disabled``), and posthog>=6 changed
    that signature to ``capture(event, **kwargs)``. Every client start and
    collection create then logged "Failed to send telemetry event ...:
    capture() takes 1 positional argument but 3 were given". Telemetry is off
    for Atlas, so the component is replaced rather than the call repaired.
    """

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None


_NOOP_TELEMETRY_IMPL = f"{NoOpProductTelemetry.__module__}.{NoOpProductTelemetry.__qualname__}"


def _get_embedding_function():
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        return SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    except Exception:
        return None


class ChromaMemoryClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = PersistentClient(
            path=settings.chroma_path,
            settings=ChromaSettings(
                anonymized_telemetry=False,
                chroma_product_telemetry_impl=_NOOP_TELEMETRY_IMPL,
                chroma_telemetry_impl=_NOOP_TELEMETRY_IMPL,
            ),
        )
        self.collection_name = "atlas_report_chunks"
        self._ef = _get_embedding_function()

    def get_collection(self) -> Collection:
        kwargs = dict(
            name=self.collection_name,
            metadata={"description": "Atlas AI chunked report memory"},
        )
        if self._ef is not None:
            kwargs["embedding_function"] = self._ef

        try:
            collection = self.client.get_or_create_collection(**kwargs)
        except (KeyError, ValueError) as exc:
            # The collection's persisted embedding-function config no longer
            # resolves against the currently-configured embedding function —
            # e.g. it was created (or last fetched) while sentence-transformers
            # failed to load, so it has no/mismatched EF config on disk, and
            # Chroma's config-reconciliation step can't validate it (surfaces
            # as KeyError('_type') from chromadb's internal schema lookup).
            # Rebuild the collection under the *current* EF rather than 500ing
            # every single request against a permanently poisoned collection.
            logger.error(
                "Chroma collection config mismatch — recreating collection",
                extra={"collection_name": self.collection_name, "error": str(exc)},
            )
            try:
                self.client.delete_collection(self.collection_name)
            except Exception:
                pass  # collection may not actually exist server-side; fine either way
            collection = self.client.get_or_create_collection(**kwargs)

        logger.info(
            "Chroma collection ready",
            extra={"collection_name": self.collection_name, "count": collection.count()},
        )
        return collection