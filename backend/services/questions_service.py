"""
"Questions I Still Need to Answer" — derives follow-up research questions
from a report's ALREADY-COMPUTED verification claims and sources.

Hard rule: every question here must be traceable to something the pipeline
actually produced (a claim's status/confidence, a source's date/domain/
count). Nothing is invented — if a report doesn't have verification data or
sources, the corresponding category of question is simply not generated. An
empty list is a correct, honest result, not a failure.

This is deliberately rule-based rather than another LLM call: the "do not
fabricate" requirement is much easier to guarantee for logic that can only
ever branch on real stored fields than for a second generative step that
would need its own grounding/fact-checking on top of what verification.py
already did.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

# A claim/report is only flagged as "possibly under-evidenced" when its
# confidence is below this — matches the sort of thing a careful reader
# would want to double check, without being so strict that every claim
# spawns a question.
LOW_CONFIDENCE_THRESHOLD = 0.6

# Reports built from fewer sources than this trigger a "is there more
# evidence" question — this mirrors the sprint's own "10 sources minimum
# for substantive research" guidance without hardcoding a mode name.
THIN_SOURCE_COUNT_THRESHOLD = 10

# A report where the newest source is older than this (days) triggers a
# recency question. ~18 months — old enough that "is this still current"
# is a fair question, not old enough to flag routine historical research.
STALE_SOURCE_DAYS = 548


def _question_id(report_id: str, category: str, discriminator: str) -> str:
    """Stable across repeated GETs for the same report/content (not
    persisted — just a deterministic key derived from what caused the
    question), so a frontend can use it as a React key / dedupe token."""
    raw = f"{report_id}:{category}:{discriminator}"
    return f"q_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _parse_claims(verification_json: str | None) -> list[dict[str, Any]]:
    if not verification_json:
        return []
    try:
        parsed = json.loads(verification_json)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed.get("claims", []) if isinstance(parsed, dict) else []


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def derive_unanswered_questions(
    report_row: dict[str, Any],
    source_rows: list[dict[str, Any]],
    *,
    now: date | None = None,
) -> list[dict[str, Any]]:
    """Returns a list of question dicts:
        {question_id, question_text, category, rationale,
         related_claim_id, related_source_ids}

    `report_row` is whatever ReportRepository.get_report() returns (has
    report_id, topic, verification_json, verification_score, ...).
    `source_rows` is whatever SourceRepository.get_sources_by_report_id()
    returns. Both are read-only here — this never writes anything.
    """
    now = now or date.today()
    report_id = report_row.get("report_id", "")
    topic = report_row.get("topic", "this topic")
    questions: list[dict[str, Any]] = []

    claims = _parse_claims(report_row.get("verification_json"))

    # ── From verification claims ────────────────────────────────────────
    for claim in claims:
        claim_id = claim.get("claim_id") or claim.get("claim_text", "")[:40]
        claim_text = (claim.get("claim_text") or "").strip()
        status = claim.get("status")
        confidence = claim.get("confidence", 0.0) or 0.0
        evidence = claim.get("evidence") or []
        source_ids = [e.get("source_id") for e in evidence if e.get("source_id")]

        if not claim_text:
            continue

        if status == "conflicting":
            questions.append({
                "question_id": _question_id(report_id, "conflicting_evidence", claim_id),
                "question_text": f"Sources disagree on this — which side has stronger support: \"{claim_text}\"?",
                "category": "conflicting_evidence",
                "rationale": "Verification found conflicting evidence across sources for this claim.",
                "related_claim_id": claim_id,
                "related_source_ids": source_ids,
            })
        elif status in ("not_verified", "insufficient_evidence"):
            questions.append({
                "question_id": _question_id(report_id, "insufficient_evidence", claim_id),
                "question_text": f"Is there stronger primary evidence for this claim: \"{claim_text}\"?",
                "category": "insufficient_evidence",
                "rationale": f"This claim's verification status is '{status}' — the evidence gathered wasn't enough to confirm it.",
                "related_claim_id": claim_id,
                "related_source_ids": source_ids,
            })
        elif status == "partially_verified" and confidence < LOW_CONFIDENCE_THRESHOLD:
            questions.append({
                "question_id": _question_id(report_id, "low_confidence", claim_id),
                "question_text": f"What are the limitations of the evidence behind this claim: \"{claim_text}\"?",
                "category": "low_confidence",
                "rationale": f"This claim is only partially verified, with a confidence score of {confidence:.2f}.",
                "related_claim_id": claim_id,
                "related_source_ids": source_ids,
            })

    # ── From source coverage ────────────────────────────────────────────
    source_count = len(source_rows)
    if source_count and source_count < THIN_SOURCE_COUNT_THRESHOLD:
        questions.append({
            "question_id": _question_id(report_id, "thin_sources", str(source_count)),
            "question_text": f"Are there additional sources that would strengthen the evidence on {topic}?",
            "category": "thin_sources",
            "rationale": f"This report drew on only {source_count} source(s).",
            "related_claim_id": None,
            "related_source_ids": [s.get("source_id") for s in source_rows if s.get("source_id")],
        })

    # ── From source recency ─────────────────────────────────────────────
    publish_dates = [d for d in (_parse_date(s.get("publish_date")) for s in source_rows) if d]
    if source_rows and not publish_dates:
        questions.append({
            "question_id": _question_id(report_id, "unknown_recency", "no_dates"),
            "question_text": f"How recent is the evidence behind {topic}?",
            "category": "unknown_recency",
            "rationale": "None of the sources used have a known publish date.",
            "related_claim_id": None,
            "related_source_ids": [],
        })
    elif publish_dates:
        newest = max(publish_dates)
        if (now - newest).days > STALE_SOURCE_DAYS:
            questions.append({
                "question_id": _question_id(report_id, "stale_sources", newest.isoformat()),
                "question_text": f"Has newer research emerged on {topic} since {newest.isoformat()}?",
                "category": "stale_sources",
                "rationale": f"The most recent source used was published on {newest.isoformat()}.",
                "related_claim_id": None,
                "related_source_ids": [],
            })

    # ── From source diversity ───────────────────────────────────────────
    domains = {s.get("domain") for s in source_rows if s.get("domain")}
    if source_count >= 2 and len(domains) == 1:
        (only_domain,) = domains
        questions.append({
            "question_id": _question_id(report_id, "single_source_domain", only_domain),
            "question_text": f"Do independent sources corroborate this, or does it rely entirely on {only_domain}?",
            "category": "single_source_domain",
            "rationale": f"All {source_count} sources used come from the same domain ({only_domain}).",
            "related_claim_id": None,
            "related_source_ids": [s.get("source_id") for s in source_rows if s.get("source_id")],
        })

    # ── From the overall verdict ────────────────────────────────────────
    verdict = (report_row.get("verification_verdict") or "").strip()
    score = report_row.get("verification_score")
    if verdict and score is not None:
        normalized_score = score / 100.0 if score > 1.0 else score
        if normalized_score < LOW_CONFIDENCE_THRESHOLD:
            questions.append({
                "question_id": _question_id(report_id, "weak_overall_verdict", verdict),
                "question_text": f"Given the overall verification verdict (\"{verdict}\"), what would it take to raise confidence in this research?",
                "category": "weak_overall_verdict",
                "rationale": f"Overall verification score is {normalized_score:.2f}, with verdict '{verdict}'.",
                "related_claim_id": None,
                "related_source_ids": [],
            })

    return questions
