"""Standard IR metrics so retrieval quality is measurable (M5).

All functions take the ranked list of retrieved doc ids and the set of relevant
doc ids (binary relevance) and return values in [0, 1].
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / k if k > 0 else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for pos, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / pos
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(pos + 1) for pos, d in enumerate(ranked[:k], start=1) if d in relevant)
    ideal = sum(1.0 / math.log2(pos + 1) for pos in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def evaluate_rankings(cases: Mapping[str, tuple[Sequence[str], Iterable[str]]], k: int = 3) -> dict[str, float]:
    """``cases``: query -> (ranked doc ids, relevant doc ids). Returns macro averages."""
    if not cases:
        return {"recall@k": 0.0, "precision@k": 0.0, "mrr": 0.0, "ndcg@k": 0.0}
    n = len(cases)
    rows = [(list(r), set(rel)) for r, rel in cases.values()]
    return {
        "recall@k": sum(recall_at_k(r, rel, k) for r, rel in rows) / n,
        "precision@k": sum(precision_at_k(r, rel, k) for r, rel in rows) / n,
        "mrr": sum(reciprocal_rank(r, rel) for r, rel in rows) / n,
        "ndcg@k": sum(ndcg_at_k(r, rel, k) for r, rel in rows) / n,
    }
