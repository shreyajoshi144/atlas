"""Pure, deterministic classification of a challenge outcome.

Inputs are the claim's ORIGINAL evidence (what the investigation relied on) and
the NEW, provenance-checked evidence retrieved by the challenge. No I/O, no clock
(``as_of`` is passed in), so every branch is unit-testable.

Results: ``supported`` | ``weakened`` | ``contradicted`` | ``unresolved`` |
``stale`` | ``source_concern``.

Policy, in one place:
* A *strong* direct contradiction (stance ``contradict``, strength >= 0.6) that
  outweighs the supporting evidence is ``contradicted`` — for every challenge type.
* Other adverse evidence (weaker contradiction, or qualifying/``mixed`` evidence)
  is ``weakened`` unless supporting evidence clearly outweighs it (>= 2x), in
  which case the claim ``supported`` — it survived the challenge.
* ``source_quality`` can end in ``source_concern`` (all supporting sources are
  low-reputation, or the claim rests on one source with no independent
  corroboration found).
* ``temporal_validity`` can end in ``stale`` (supporting sources are all old and
  nothing newer re-validates them).
* No usable new evidence is ``unresolved`` — never silently ``supported``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

STRONG_CONTRADICTION = 0.6
OUTWEIGH_RATIO = 2.0
LOW_REPUTATION = 0.45
OLD_AFTER_DAYS = 365
_QUALIFYING_WEIGHT = {"alternative_explanation": 1.0, "assumption_check": 1.0}  # others: 0.5


@dataclass(frozen=True)
class BaseEvidence:
    """One piece of evidence the original investigation relied on."""

    url: str
    domain: str
    stance: str = "support"
    strength: float = 0.0
    publish_date: Optional[date] = None


@dataclass(frozen=True)
class NewEvidence:
    """Provenance-checked evidence found by the challenge."""

    source_id: str
    url: str
    domain: str
    stance: str            # support | contradict | mixed
    strength: float
    independent: bool = True      # domain not used by the original evidence
    publish_date: Optional[date] = None


@dataclass
class Classification:
    result: str
    rationale: str
    factors: dict = field(default_factory=dict)


def _sum(items: Iterable[NewEvidence]) -> float:
    return round(sum(max(0.0, min(1.0, e.strength)) for e in items), 4)


def _is_old(d: Optional[date], as_of: date) -> Optional[bool]:
    return None if d is None else (as_of - d) > timedelta(days=OLD_AFTER_DAYS)


def classify(
    challenge_type: str,
    *,
    base_evidence: list[BaseEvidence],
    new_evidence: list[NewEvidence],
    retrieved_sources: int,
    scraped_sources: int,
    as_of: date,
    reputation: Callable[[str], float],
) -> Classification:
    contra = [e for e in new_evidence if e.stance == "contradict"]
    supp = [e for e in new_evidence if e.stance == "support"]
    mixed = [e for e in new_evidence if e.stance == "mixed"]
    c_w, s_w, m_w = _sum(contra), _sum(supp), _sum(mixed)
    strong = [e for e in contra if e.strength >= STRONG_CONTRADICTION]
    adverse = c_w + _QUALIFYING_WEIGHT.get(challenge_type, 0.5) * m_w

    factors = {
        "challenge_type": challenge_type, "retrieved_sources": retrieved_sources, "scraped_sources": scraped_sources,
        "new_evidence": len(new_evidence), "supporting": len(supp), "contradicting": len(contra), "qualifying": len(mixed),
        "supporting_weight": s_w, "contradicting_weight": c_w, "qualifying_weight": m_w,
        "strong_contradictions": len(strong), "independent_sources": len({e.domain for e in new_evidence if e.independent}),
    }

    def done(result: str, why: str, **extra) -> Classification:
        return Classification(result, why, {**factors, **extra})

    def empty_reason() -> str:
        if retrieved_sources == 0:
            return "No new sources were found that were independent of the original evidence."
        if scraped_sources == 0:
            return "Sources were found but none could be retrieved, so nothing could be verified."
        return "Retrieved sources contained no evidence that bears on the claim."

    # ── source_quality ───────────────────────────────────────────────────────
    if challenge_type == "source_quality":
        if strong and c_w > s_w:
            return done("contradicted", "A strong, independent source directly contradicts the claim.")
        pool = [b for b in base_evidence if b.stance == "support"] or list(base_evidence)
        domains = sorted({b.domain for b in pool if b.domain})
        reps = {d: reputation(d) for d in domains}
        independent_support = [e for e in supp if e.independent and reputation(e.domain) >= LOW_REPUTATION]
        low_all = bool(domains) and all(r < LOW_REPUTATION for r in reps.values())
        single = len(domains) == 1 and len(pool) >= 1
        sq = {"original_domains": domains, "original_reputation": reps, "independent_support": len(independent_support)}
        if low_all:
            return done("source_concern", "Every source the claim rests on has a low reputation.", **sq)
        if single and not independent_support:
            return done("source_concern", "The claim rests on a single source and no independent corroboration was found.", **sq)
        if independent_support:
            if adverse > 0 and not s_w >= OUTWEIGH_RATIO * adverse:
                return done("weakened", "Independent sources corroborate, but credible qualifying/contrary evidence also exists.", **sq)
            return done("supported", "Independent, reputable sources corroborate the claim.", **sq)
        if adverse > 0:
            return done("weakened", "Contrary or qualifying evidence was found and no independent corroboration.", **sq)
        return done("unresolved", empty_reason(), **sq)

    # ── temporal_validity ────────────────────────────────────────────────────
    if challenge_type == "temporal_validity":
        dated = [b.publish_date for b in base_evidence if b.publish_date]
        newest = max(dated) if dated else None
        old_flags = [_is_old(d, as_of) for d in dated]
        base_all_old = bool(dated) and all(old_flags)
        newer = lambda e: e.publish_date is not None and (newest is None or e.publish_date > newest)  # noqa: E731
        newer_contra, newer_supp = [e for e in contra if newer(e)], [e for e in supp if newer(e)]
        undated_or_newer_adverse = [e for e in contra + mixed if newer(e) or e.publish_date is None]
        tv = {"newest_original_date": newest.isoformat() if newest else None, "original_all_old": base_all_old,
              "newer_supporting": len(newer_supp), "newer_contradicting": len(newer_contra)}
        if [e for e in newer_contra if e.strength >= STRONG_CONTRADICTION] and _sum(newer_contra) > _sum(newer_supp):
            return done("contradicted", "Newer evidence directly contradicts the claim.", **tv)
        if undated_or_newer_adverse:
            return done("weakened", "Newer or undated evidence contradicts or qualifies the claim.", **tv)
        if newer_supp:
            return done("supported", "Newer evidence re-validates the claim.", **tv)
        if base_all_old:
            return done("stale", f"All dated supporting sources are older than {OLD_AFTER_DAYS} days and nothing newer re-validates them.", **tv)
        if dated:
            return done("supported", "Supporting sources are recent and nothing newer contradicts them.", **tv)
        return done("unresolved", "The supporting sources have no publish dates, so temporal validity cannot be judged.", **tv)

    # ── counter_evidence / contradiction_search / alternative_explanation / assumption_check ──
    if not new_evidence:
        return done("unresolved", empty_reason())
    if strong and c_w > s_w:
        return done("contradicted", "Independent evidence directly contradicts the claim and outweighs the support found.")
    if adverse > 0:
        if s_w >= OUTWEIGH_RATIO * adverse and not strong:
            return done("supported", "Counter-evidence was found but is clearly outweighed by supporting evidence.", adverse_weight=round(adverse, 4))
        return done("weakened", "Credible contrary or qualifying evidence reduces confidence in the claim.", adverse_weight=round(adverse, 4))
    if supp:
        return done("supported", "The challenge found only supporting evidence and no counter-evidence.")
    return done("unresolved", empty_reason())


# ── updated interpretation ───────────────────────────────────────────────────
_PRECEDENCE = ("contradicted", "weakened", "stale", "source_concern")


def interpret(base_status: str, base_confidence: float, challenges: list[dict]) -> dict:
    """Derive the claim's *updated interpretation* from its original observation
    plus completed challenges. Pure and non-destructive: the original
    observation is never modified; this is a view over it.

    Only the latest completed challenge per type counts. ``adjusted_confidence``
    is a transparent heuristic, not a probability.
    """
    latest: dict[str, dict] = {}
    for ch in sorted((c for c in challenges if c.get("status") == "completed" and c.get("result")),
                     key=lambda c: (c.get("completed_at") or "", c.get("challenge_id") or "")):
        latest[ch["challenge_type"]] = ch
    results = [c["result"] for c in latest.values()]
    counts = {r: results.count(r) for r in ("supported", "weakened", "contradicted", "unresolved", "stale", "source_concern")}

    if not results:
        label, summary = "unchallenged", "This claim has not been challenged."
    else:
        label = next((r for r in _PRECEDENCE if counts[r]), None) or ("supported" if counts["supported"] else "unresolved")
        types = lambda r: ", ".join(sorted(t.replace("_", " ") for t, c in latest.items() if c["result"] == r))  # noqa: E731
        summary = {
            "contradicted": f"Contradicted by challenge evidence ({types('contradicted')}).",
            "weakened": f"Weakened by challenge evidence ({types('weakened')}).",
            "stale": f"May be out of date ({types('stale')}).",
            "source_concern": f"Source concerns raised ({types('source_concern')}).",
            "supported": f"Survived {counts['supported']} challenge(s) ({types('supported')}).",
            "unresolved": "Challenges did not find enough evidence to change the interpretation.",
        }[label]

    penalty = (0.40 if counts["contradicted"] else 0.0) + 0.15 * min(counts["weakened"], 2) \
        + (0.10 if counts["stale"] else 0.0) + (0.10 if counts["source_concern"] else 0.0)
    bonus = 0.05 * min(counts["supported"], 2) if penalty == 0 else 0.0
    adjusted = round(max(0.0, min(1.0, float(base_confidence or 0.0) - penalty + bonus)), 4)
    return {
        "label": label, "summary": summary, "original_status": base_status,
        "original_confidence": float(base_confidence or 0.0), "adjusted_confidence": adjusted,
        "counts": counts, "basis": [c["challenge_id"] for c in latest.values()],
    }
