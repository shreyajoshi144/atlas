"""Hybrid retrieval (M5): BM25 + dense similarity + metadata filters + reranking.

Pure Python and import-light: importing this package loads no embedding model,
vector database or network client.
"""
from backend.retrieval.bm25 import BM25Index
from backend.retrieval.config import HybridConfig, load_hybrid_config
from backend.retrieval.filters import InvalidFilterError, matches_filter, validate_filter
from backend.retrieval.hybrid import Embedder, HybridRetriever
from backend.retrieval.models import (
    RetrievalDiagnostics,
    RetrievalDocument,
    RetrievalHit,
    RetrievalResult,
)
from backend.retrieval.rerank import FeatureReranker, Reranker

__all__ = [
    "BM25Index",
    "Embedder",
    "FeatureReranker",
    "HybridConfig",
    "HybridRetriever",
    "InvalidFilterError",
    "Reranker",
    "RetrievalDiagnostics",
    "RetrievalDocument",
    "RetrievalHit",
    "RetrievalResult",
    "load_hybrid_config",
    "matches_filter",
    "validate_filter",
]
