"""M6 — pure freshness / staleness / change-classification logic."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.knowledge.freshness import (
    AGING_DAYS, CHANGE_THRESHOLD, FRESH_DAYS, ClaimSnapshot, SupportingSource, assess_staleness,
    classify_change, parse_dt, source_freshness, worst_state,
)

AS_OF = datetime(2026, 10, 4, tzinfo=timezone.utc)


def days_ago(n: int) -> datetime:
    return AS_OF - timedelta(days=n)


# ── parsing ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["2026-01-10T09:00:00+00:00", "2026-01-10T09:00:00Z", "2026-01-10", date(2026, 1, 10), datetime(2026, 1, 10)])
def test_parse_dt_accepts_common_forms_and_is_utc_aware(value):
    dt = parse_dt(value)
    assert dt.tzinfo is not None and (dt.year, dt.month, dt.day) == (2026, 1, 10)


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_dt_returns_none_for_unparseable(value):
    assert parse_dt(value) is None


# ── knowledge state ──────────────────────────────────────────────────────────
def test_worst_state_ordering_and_empty_is_never_complete():
    assert worst_state(["complete", "partial", "failed"]) == "failed"
    assert worst_state(["complete", "partial"]) == "partial"
    assert worst_state(["complete", None]) == "not_recorded"
    assert worst_state(["complete", "complete"]) == "complete"
    assert worst_state([]) == "not_recorded"  # no evidence of completeness ≠ complete


# ── source freshness ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("age,label", [(0, "fresh"), (FRESH_DAYS, "fresh"), (FRESH_DAYS + 1, "aging"), (AGING_DAYS, "aging"), (AGING_DAYS + 1, "old")])
def test_source_freshness_buckets(age, label):
    assert source_freshness(days_ago(age), as_of=AS_OF)["label"] == label


def test_unknown_publish_date_is_unknown_not_guessed():
    f = source_freshness(None, as_of=AS_OF)
    assert f == {"label": "unknown", "age_days": None, "published": None}


def test_future_publish_date_does_not_produce_negative_age():
    assert source_freshness(AS_OF + timedelta(days=5), as_of=AS_OF)["age_days"] == 0


# ── staleness ────────────────────────────────────────────────────────────────
def test_recent_claim_with_fresh_sources_is_not_stale():
    a = assess_staleness(days_ago(5), [SupportingSource("s", days_ago(10))], as_of=AS_OF)
    assert not a.is_stale and a.reasons == []


def test_age_reason_boundary():
    assert not assess_staleness(days_ago(90), [], as_of=AS_OF, stale_after_days=90).is_stale
    assert assess_staleness(days_ago(91), [], as_of=AS_OF, stale_after_days=90).codes == ["age"]


def test_sources_old_requires_all_known_dates_old():
    old = days_ago(AGING_DAYS + 50)
    assert assess_staleness(days_ago(1), [SupportingSource("a", old), SupportingSource("b", old)], as_of=AS_OF).codes == ["sources_old"]
    assert not assess_staleness(days_ago(1), [SupportingSource("a", old), SupportingSource("b", days_ago(5))], as_of=AS_OF).is_stale
    # an unknown publish date must never be treated as "old"
    assert not assess_staleness(days_ago(1), [SupportingSource("a", old), SupportingSource("b", None)], as_of=AS_OF).is_stale
    assert not assess_staleness(days_ago(1), [SupportingSource("a", None)], as_of=AS_OF).is_stale


def test_source_changed_reason_and_multiple_reasons_combine():
    a = assess_staleness(days_ago(1), [SupportingSource("a", days_ago(5), changed_since_verified=True)], as_of=AS_OF)
    assert a.codes == ["source_changed"]
    combo = assess_staleness(days_ago(400), [SupportingSource("a", days_ago(900), changed_since_verified=True)], as_of=AS_OF)
    assert set(combo.codes) == {"age", "sources_old", "source_changed"} and combo.is_stale


def test_no_observation_time_and_no_sources_is_not_flagged_by_guesswork():
    assert not assess_staleness(None, [], as_of=AS_OF).is_stale


def test_staleness_is_deterministic():
    args = (days_ago(200), [SupportingSource("a", days_ago(500))])
    assert assess_staleness(*args, as_of=AS_OF).as_dict() == assess_staleness(*args, as_of=AS_OF).as_dict()


# ── change classification ────────────────────────────────────────────────────
def snap(status="verified", conf=0.8, support=1, conflict=0):
    return ClaimSnapshot("clm_x", status, conf, support, conflict)


def test_unchanged_when_identical():
    assert classify_change(snap(), snap())[0] == "unchanged"


def test_strengthened_by_confidence_status_and_support():
    assert classify_change(snap("partially_verified", 0.5), snap("verified", 0.9, support=2))[0] == "strengthened"
    assert classify_change(snap(conf=0.5), snap(conf=0.7))[0] == "strengthened"


def test_weakened_by_confidence_drop():
    label, reasons = classify_change(snap(conf=0.9), snap("partially_verified", 0.4))
    assert label == "weakened" and any("confidence" in r for r in reasons) and any("status" in r for r in reasons)


def test_contradicted_when_status_turns_conflicting_or_conflict_evidence_appears():
    assert classify_change(snap(), snap("conflicting", 0.3, support=0, conflict=1))[0] == "contradicted"
    label, reasons = classify_change(snap(conf=0.8), snap(conf=0.9, support=2, conflict=1))
    assert label == "contradicted" and "contradicting evidence" in reasons[0]  # even though confidence rose


def test_already_conflicting_claim_is_not_re_reported_as_contradicted_by_status_alone():
    assert classify_change(snap("conflicting", 0.3, 0, 1), snap("conflicting", 0.3, 0, 1))[0] == "unchanged"


def test_small_changes_below_threshold_are_unchanged():
    assert classify_change(snap(conf=0.80), snap(conf=0.80 + CHANGE_THRESHOLD / 2))[0] == "unchanged"


def test_swapping_base_and_target_flips_strengthened_and_weakened():
    a, b = snap("partially_verified", 0.5), snap("verified", 0.9, support=2)
    assert classify_change(a, b)[0] == "strengthened"
    assert classify_change(b, a)[0] == "weakened"


def test_classification_is_deterministic():
    a, b = snap(conf=0.9), snap(conf=0.4)
    assert classify_change(a, b) == classify_change(a, b)
