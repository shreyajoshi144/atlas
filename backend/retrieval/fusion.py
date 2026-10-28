"""Rank fusion for combining first-stage retrievers."""
from __future__ import annotations

from typing import Mapping, Sequence


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[int]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = 60,
) -> dict[int, float]:
    """Weighted Reciprocal Rank Fusion (Cormack et al., 2009).

    ``rankings`` maps a retriever name to document indices ordered best-first.
    Each document earns ``weight / (k + rank)`` (rank is 1-based) from every
    retriever that ranked it. RRF needs no score calibration between a BM25 and
    a cosine scale, which is why it is the fusion used here.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1")
    fused: dict[int, float] = {}
    for name, ordered in rankings.items():
        weight = 1.0 if weights is None else float(weights.get(name, 1.0))
        if weight <= 0.0:
            continue
        for position, doc_index in enumerate(ordered, start=1):
            fused[doc_index] = fused.get(doc_index, 0.0) + weight / (k + position)
    return fused


def max_possible_rrf(weights: Sequence[float], k: int) -> float:
    """Score of a document ranked first by every contributing retriever."""
    return sum(w for w in weights if w > 0.0) / (k + 1)
