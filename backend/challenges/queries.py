"""Deterministic challenge-query construction.

No LLM writes these: each challenge type maps to a fixed set of adversarial
focus phrases that are combined with the claim text by the existing
``request_more_evidence`` tool. That keeps the search surface predictable,
reviewable and injection-resistant.
"""
from __future__ import annotations

from backend.agentic.common import MAX_QUERY_CHARS
from backend.challenges.models import CHALLENGE_TYPES, MAX_QUERIES

FOCUS = {
    "counter_evidence": ("evidence against criticism rebuttal", "counterexamples disputed"),
    "contradiction_search": ("false incorrect debunked", "contradicts conflicting findings"),
    "source_quality": ("independent confirmation", "primary source original data"),
    "temporal_validity": ("latest update {year}", "outdated superseded revised"),
    "assumption_check": ("assumptions limitations caveats", "only holds under certain conditions"),
    "alternative_explanation": ("alternative explanation", "other causes confounding factors"),
}
assert set(FOCUS) == set(CHALLENGE_TYPES)


def build_queries(challenge_type: str, claim_text: str, *, year: int, max_queries: int = MAX_QUERIES) -> list[dict[str, str]]:
    """Return ``[{"question", "focus"}]``; ``question`` is the (bounded) claim."""
    if challenge_type not in FOCUS:
        raise ValueError(f"unknown challenge type: {challenge_type!r}")
    claim = " ".join(claim_text.split())
    if len(claim) > MAX_QUERY_CHARS - 60:
        claim = claim[: MAX_QUERY_CHARS - 60].rsplit(" ", 1)[0]
    return [{"question": claim, "focus": f.format(year=year)} for f in FOCUS[challenge_type][:max_queries]]
