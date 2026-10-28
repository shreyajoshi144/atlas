"""Small statistics helpers (no numpy)."""
from __future__ import annotations

import math


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate."""
    if n <= 0:
        return (0.0, 1.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, round(centre - half, 4)), min(1.0, round(centre + half, 4)))


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def separated(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when two confidence intervals do not overlap."""
    return a[0] > b[1] or b[0] > a[1]
