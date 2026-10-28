from __future__ import annotations

from collections import Counter

from backend.evaluation.models import EvalFlag, VerificationQualityEvaluation

VALID_STANCES = {"support", "contradict", "mixed"}

# Statuses where a LOW confidence would be internally inconsistent
# (the LLM said "verified" but wasn't confident about it).
_EXPECT_HIGHER_CONFIDENCE = {"verified"}
_LOW_CONFIDENCE_CUTOFF = 0.5

# Statuses where a HIGH confidence would be internally inconsistent
# (the LLM said it couldn't verify/find evidence, yet was very confident).
_EXPECT_LOWER_CONFIDENCE = {"not_verified", "insufficient_evidence"}
_HIGH_CONFIDENCE_CUTOFF = 0.8


def evaluate_verification_quality(
    verification_summary,
    verification_status: str,
) -> VerificationQualityEvaluation:
    """Checks the verification agent's *own* output for internal
    consistency — it does not re-derive verdicts, only flags outputs that
    contradict themselves (e.g. "verified" with no evidence, or a stance
    value outside the documented enum)."""
    flags: list[EvalFlag] = []

    if verification_status != "succeeded" or verification_summary is None:
        severity = "warning" if verification_status == "failed" else "info"
        flags.append(
            EvalFlag(
                code="verification_unavailable",
                severity=severity,
                message=f"Verification step status was '{verification_status}'.",
            )
        )
        return VerificationQualityEvaluation(score=0.0, flags=flags)

    claims = list(getattr(verification_summary, "claims", None) or [])
    claim_count = len(claims)
    status_counts = Counter(str(getattr(c, "status", "")) for c in claims)

    mismatches = 0
    invalid_stances = 0
    empty_evidence_verified = 0

    for claim in claims:
        status = str(getattr(claim, "status", ""))
        confidence = float(getattr(claim, "confidence", 0.0))
        evidence = list(getattr(claim, "evidence", None) or [])

        if status in _EXPECT_HIGHER_CONFIDENCE and confidence < _LOW_CONFIDENCE_CUTOFF:
            mismatches += 1
        if status in _EXPECT_LOWER_CONFIDENCE and confidence > _HIGH_CONFIDENCE_CUTOFF:
            mismatches += 1
        if status == "verified" and not evidence:
            empty_evidence_verified += 1
        for item in evidence:
            if getattr(item, "stance", "") not in VALID_STANCES:
                invalid_stances += 1

    if empty_evidence_verified:
        flags.append(
            EvalFlag(
                code="verified_without_evidence",
                severity="critical",
                message=f"{empty_evidence_verified} claim(s) are marked 'verified' with no supporting evidence attached.",
            )
        )
    if mismatches:
        flags.append(
            EvalFlag(
                code="confidence_status_mismatch",
                severity="warning",
                message=f"{mismatches} claim(s) have a confidence score inconsistent with their status.",
            )
        )
    if invalid_stances:
        flags.append(
            EvalFlag(
                code="invalid_stance",
                severity="warning",
                message=f"{invalid_stances} evidence item(s) had a stance value outside support/contradict/mixed.",
            )
        )

    # Consistency ratio: 1.0 minus a weighted penalty per inconsistency,
    # normalized by claim count so the score doesn't just track claim volume.
    penalty = mismatches + (empty_evidence_verified * 2) + invalid_stances
    consistency_ratio = 1.0 - (penalty / max(claim_count, 1)) / 3
    consistency_ratio = max(0.0, min(1.0, consistency_ratio))

    normalized_overall = None
    raw_overall = getattr(verification_summary, "overall_verification_score", None)
    if raw_overall is not None:
        normalized_overall = max(0.0, min(1.0, raw_overall / 100.0))

    score = round(
        0.5 * consistency_ratio + 0.5 * (normalized_overall if normalized_overall is not None else consistency_ratio),
        4,
    )

    return VerificationQualityEvaluation(
        overall_verification_score=raw_overall,
        claim_count=claim_count,
        status_distribution=dict(status_counts),
        confidence_status_mismatches=mismatches,
        invalid_stance_count=invalid_stances,
        empty_evidence_verified_count=empty_evidence_verified,
        score=score,
        flags=flags,
    )
