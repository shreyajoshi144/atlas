"""Retrieval tuning loaded from Settings, with safe fallbacks."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HybridConfig:
    rrf_k: int = 60
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    candidate_depth: int = 50          # per first-stage retriever
    rerank_depth: int = 30             # fused candidates handed to the reranker
    min_dense_similarity: float = 0.0  # cosine floor for a dense candidate
    mmr_lambda: float = 0.7            # relevance-vs-diversity trade-off

    def __post_init__(self) -> None:
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be >= 1")
        if self.lexical_weight < 0 or self.dense_weight < 0:
            raise ValueError("retriever weights must be >= 0")
        if self.candidate_depth < 1 or self.rerank_depth < 1:
            raise ValueError("candidate/rerank depth must be >= 1")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError("mmr_lambda must be within [0, 1]")


def load_hybrid_config() -> HybridConfig:
    """Build the config from Settings; fall back to defaults if Settings can't load
    (e.g. a unit test without provider keys) so retrieval never depends on them."""
    try:
        from backend.core.config import get_settings

        s = get_settings()
        return HybridConfig(
            rrf_k=s.retrieval_rrf_k,
            lexical_weight=s.retrieval_lexical_weight,
            dense_weight=s.retrieval_dense_weight,
            candidate_depth=s.retrieval_candidate_depth,
        )
    except Exception:
        return HybridConfig()
