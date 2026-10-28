from __future__ import annotations

import math
import re
from collections import Counter

# Same tokenization approach as backend/services/ranking_service.py — kept
# identical on purpose so evaluation scores are comparable to the retrieval
# scores the pipeline already computes, and so no new dependency (e.g. numpy,
# sklearn) is introduced for what is meant to be a lightweight layer.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")
# Trailing characters that are almost always sentence/list punctuation
# rather than part of the URL itself (e.g. "see https://x.com/a, and https://x.com/b."
# must not extract "https://x.com/a," with the comma attached).
_URL_TRAILING_PUNCTUATION = ".,;:!?"


def tokenize(text: str | None) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def token_set(text: str | None) -> set[str]:
    return set(tokenize(text))


def cosine_similarity(a: str | None, b: str | None) -> float:
    """Bag-of-words cosine similarity in [0, 1]."""
    tokens_a, tokens_b = tokenize(a), tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0

    counts_a, counts_b = Counter(tokens_a), Counter(tokens_b)
    vocabulary = set(counts_a) | set(counts_b)

    dot = sum(counts_a[t] * counts_b[t] for t in vocabulary)
    norm_a = math.sqrt(sum(v * v for v in counts_a.values()))
    norm_b = math.sqrt(sum(v * v for v in counts_b.values()))

    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def containment_ratio(needle: str | None, haystack: str | None) -> float:
    """Fraction of `needle`'s informative (len >= 4) tokens present in `haystack`.

    Used to check whether a short claim/evidence snippet is actually
    grounded in a larger evidence corpus, without requiring exact substring
    matches (LLM output rarely reproduces text verbatim).
    """
    needle_tokens = [t for t in tokenize(needle) if len(t) >= 4]
    if not needle_tokens:
        # Nothing substantive to check against — treat as vacuously grounded
        # rather than penalizing short/empty snippets.
        return 1.0
    haystack_tokens = token_set(haystack)
    if not haystack_tokens:
        return 0.0
    matched = sum(1 for t in needle_tokens if t in haystack_tokens)
    return matched / len(needle_tokens)


def extract_urls(text: str | None) -> list[str]:
    raw = _URL_RE.findall(text or "")
    return [u.rstrip(_URL_TRAILING_PUNCTUATION) for u in raw]


def normalize_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()
