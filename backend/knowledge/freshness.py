"""Pure freshness, staleness and claim-strength logic for the Knowledge Map.

Everything here is a deterministic function of its inputs and an explicit
``as_of`` time — never the wall clock — so "what changed" answers and stale
flags are reproducible and idempotent.

Vocabulary (kept in lock-step with ``VerificationStatus``):
``verified > partially_verified > insufficient_evidence > not_verified >
conflicting`` ranks how strongly a claim is supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional

STATUS_RANK = {
    "verified": 4,
    "partially_verified": 3,
    "insufficient_evidence": 2,
    "not_verified": 1,
    "conflicting": 0,
}

# Source content age buckets (days since publish date).
FRESH_DAYS = 90
AGING_DAYS = 365

# Staleness defaults.
DEFAULT_STALE_AFTER_DAYS = 90        # claim not re-verified for this long
DEFAULT_SOURCE_MAX_AGE_DAYS = 365    # all supporting sources older than this

# Knowledge-state vocabulary, worst first.
STATE_ORDER = ("failed", "not_recorded", "partial", "complete")


def parse_dt(value: object) -> Optional[datetime]:
    """Parse an ISO datetime/date (str, date, datetime) to an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_days(then: Optional[datetime], as_of: datetime) -> Optional[int]:
    if then is None:
        return None
    return max(0, (as_of - then).days)


def worst_state(states: Iterable[Optional[str]]) -> str:
    """Worst knowledge state in ``states``; empty → ``not_recorded`` (never 'complete')."""
    seen = {s or "not_recorded" for s in states}
    for state in STATE_ORDER:
        if state in seen:
            return state
    return "not_recorded"


def source_freshness(publish_date: object, *, as_of: datetime) -> dict:
    """Freshness of a source's *content*, from its publish date only.

    An unknown publish date yields ``unknown`` — it is never guessed from the
    time Atlas happened to fetch the page.
    """
    published = parse_dt(publish_date)
    days = age_days(published, as_of)
    if days is None:
        return {"label": "unknown", "age_days": None, "published": None}
    label = "fresh" if days <= FRESH_DAYS else "aging" if days <= AGING_DAYS else "old"
    return {"label": label, "age_days": days, "published": published.date().isoformat()}


@dataclass(frozen=True)
class SupportingSource:
    source_id: str
    publish_date: object = None
    changed_since_verified: bool = False  # content hash differs from a later fetch


@dataclass
class StaleAssessment:
    is_stale: bool
    reasons: list[dict] = field(default_factory=list)  # [{code, message}]

    @property
    def codes(self) -> list[str]:
        return [r["code"] for r in self.reasons]

    def as_dict(self) -> dict:
        return {"is_stale": self.is_stale, "reasons": list(self.reasons)}


def assess_staleness(
    last_observed_at: Optional[datetime],
    supporting_sources: Iterable[SupportingSource],
    *,
    as_of: datetime,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    source_max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS,
) -> StaleAssessment:
    """Why (if at all) a claim should be re-checked.

    Reasons, each independent:
    * ``age`` — not re-verified for more than ``stale_after_days``.
    * ``sources_old`` — every supporting source has a KNOWN publish date older
      than ``source_max_age_days`` (an unknown date never triggers this).
    * ``source_changed`` — a supporting source's content hash changed in a
      later investigation, so the evidence may no longer say what it said.
    """
    reasons: list[dict] = []
    days = age_days(last_observed_at, as_of)
    if days is not None and days > stale_after_days:
        reasons.append({"code": "age", "message": f"Not re-verified for {days} days (limit {stale_after_days})."})

    sources = list(supporting_sources)
    if sources:
        ages = [age_days(parse_dt(s.publish_date), as_of) for s in sources]
        if all(a is not None and a > source_max_age_days for a in ages):
            reasons.append({"code": "sources_old", "message": f"All supporting sources are older than {source_max_age_days} days."})
        changed = [s.source_id for s in sources if s.changed_since_verified]
        if changed:
            reasons.append({"code": "source_changed", "message": f"{len(changed)} supporting source(s) changed since this claim was verified."})
    return StaleAssessment(is_stale=bool(reasons), reasons=reasons)


@dataclass(frozen=True)
class ClaimSnapshot:
    """One claim as observed by one investigation."""

    claim_id: str
    status: str
    confidence: float
    support: int = 0
    conflict: int = 0
    evidence: tuple = ()  # ((evidence_id, stance, strength, source_id), ...)

    @property
    def status_rank(self) -> int:
        return STATUS_RANK.get(self.status, 2)

    @property
    def net_support(self) -> int:
        return self.support - self.conflict


# A change score at or beyond this magnitude is a real strengthening/weakening.
CHANGE_THRESHOLD = 0.10


def classify_change(base: ClaimSnapshot, target: ClaimSnapshot) -> tuple[str, list[str]]:
    """Classify base→target for a claim present in both: returns (class, reasons).

    Classes: ``contradicted`` | ``strengthened`` | ``weakened`` | ``unchanged``.

    ``contradicted`` wins: the target is ``conflicting`` (when the base was not),
    or contradicting evidence appeared where there was none. Otherwise one score
    combines confidence, status rank and net evidence support, so the result is
    a single explainable number rather than a pile of ad-hoc rules.
    """
    reasons: list[str] = []
    if target.status == "conflicting" and base.status != "conflicting":
        reasons.append("status became conflicting")
    if target.conflict > 0 and base.conflict == 0:
        reasons.append(f"{target.conflict} contradicting evidence item(s) appeared")
    if reasons:
        return "contradicted", reasons

    conf_delta = round(target.confidence - base.confidence, 4)
    rank_delta = target.status_rank - base.status_rank
    net_delta = target.net_support - base.net_support
    score = conf_delta + 0.25 * rank_delta + 0.05 * net_delta

    if conf_delta:
        reasons.append(f"confidence {base.confidence:.2f} → {target.confidence:.2f}")
    if rank_delta:
        reasons.append(f"status {base.status} → {target.status}")
    if net_delta:
        reasons.append(f"net supporting evidence {base.net_support:+d} → {target.net_support:+d}")

    if score >= CHANGE_THRESHOLD:
        return "strengthened", reasons
    if score <= -CHANGE_THRESHOLD:
        return "weakened", reasons
    return "unchanged", reasons
