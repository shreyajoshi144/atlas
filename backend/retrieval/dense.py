"""Dense-vector similarity helpers (pure Python, no numpy requirement)."""
from __future__ import annotations

import math
from typing import Optional, Sequence


def cosine_similarity(a: Sequence[float] | None, b: Sequence[float] | None) -> Optional[float]:
    """Cosine of two vectors, or ``None`` when it is not defined.

    ``None`` (rather than 0.0) is returned for missing, zero-norm, non-finite
    or dimension-mismatched vectors so callers can tell "no evidence" apart
    from "orthogonal" — e.g. a chunk embedded with a different model.
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return None
    value = dot / math.sqrt(norm_a * norm_b)
    if not math.isfinite(value):
        return None
    return max(-1.0, min(1.0, value))


def dense_scores(query_vector: Sequence[float], document_vectors: Sequence[Sequence[float] | None]) -> list[Optional[float]]:
    return [cosine_similarity(query_vector, vec) for vec in document_vectors]
