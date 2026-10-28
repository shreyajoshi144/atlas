from __future__ import annotations

from backend.evaluation.models import ClaimGroundingDetail, EvalFlag, GroundingEvaluation
from backend.evaluation.text_utils import containment_ratio

# Fraction of an evidence snippet's informative tokens that must appear in
# the evidence corpus for that snippet to be considered grounded, rather
# than hallucinated by the verification LLM.
GROUNDING_OVERLAP_THRESHOLD = 0.45
_LOW_GROUNDING_RATE = 0.6


def evaluate_claim_grounding(verification_summary, evidence_text: str) -> GroundingEvaluation:
    """Cross-checks the verification agent's own claims against the actual
    evidence corpus it was given, using lexical containment rather than a
    second LLM call — this is what keeps the layer "lightweight" and unable
    to fail the research run on its own (no network calls).

    A claim's evidence is "matched" to a source when nodes.py's URL-based
    attachment succeeded (ClaimEvidence.source_id is non-empty); an empty
    source_id means the LLM cited a URL that isn't in the retrieved source
    list at all — i.e. a fabricated or mistyped citation.
    """
    flags: list[EvalFlag] = []
    claims = list(getattr(verification_summary, "claims", None) or []) if verification_summary else []

    if not claims:
        flags.append(
            EvalFlag(
                code="no_claims",
                severity="info",
                message="No claims were available to check for grounding.",
            )
        )
        return GroundingEvaluation(flags=flags)

    claim_details: list[ClaimGroundingDetail] = []
    unmatched_citations = 0
    overlaps: list[float] = []
    grounded_count = 0

    for claim in claims:
        evidence_items = list(getattr(claim, "evidence", None) or [])
        item_overlaps: list[float] = []
        grounded_evidence = 0

        for item in evidence_items:
            overlap = containment_ratio(getattr(item, "evidence_text", ""), evidence_text)
            item_overlaps.append(overlap)
            if overlap >= GROUNDING_OVERLAP_THRESHOLD:
                grounded_evidence += 1
            if not getattr(item, "source_id", ""):
                unmatched_citations += 1

        avg_overlap = round(sum(item_overlaps) / len(item_overlaps), 4) if item_overlaps else 0.0
        overlaps.append(avg_overlap)

        status = str(getattr(claim, "status", ""))
        is_grounded = (not evidence_items and status == "insufficient_evidence") or (
            bool(evidence_items) and grounded_evidence == len(evidence_items)
        )
        if is_grounded:
            grounded_count += 1

        claim_details.append(
            ClaimGroundingDetail(
                claim_id=getattr(claim, "claim_id", ""),
                claim_text=getattr(claim, "claim_text", ""),
                status=status,
                confidence=float(getattr(claim, "confidence", 0.0)),
                evidence_count=len(evidence_items),
                grounded_evidence_count=grounded_evidence,
                unmatched_source_count=len(evidence_items) - grounded_evidence,
                avg_evidence_overlap=avg_overlap,
                is_grounded=is_grounded,
            )
        )

    claim_count = len(claims)
    grounding_rate = round(grounded_count / claim_count, 4) if claim_count else 0.0
    avg_overlap_all = round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0

    if unmatched_citations:
        flags.append(
            EvalFlag(
                code="unmatched_citations",
                severity="critical",
                message=(
                    f"{unmatched_citations} evidence citation(s) reference a URL "
                    "that does not match any retrieved source."
                ),
            )
        )
    if grounding_rate < _LOW_GROUNDING_RATE:
        flags.append(
            EvalFlag(
                code="low_grounding_rate",
                severity="warning",
                message="Fewer than 60% of claims are well-grounded in the evidence corpus.",
            )
        )

    score = round(0.6 * grounding_rate + 0.4 * avg_overlap_all, 4)

    return GroundingEvaluation(
        claim_count=claim_count,
        grounded_claim_count=grounded_count,
        grounding_rate=grounding_rate,
        avg_evidence_overlap=avg_overlap_all,
        unmatched_source_citations=unmatched_citations,
        score=score,
        claim_details=claim_details,
        flags=flags,
    )
