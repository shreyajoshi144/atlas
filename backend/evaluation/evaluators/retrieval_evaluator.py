from __future__ import annotations

from typing import Sequence

from backend.evaluation.models import EvalFlag, RetrievalEvaluation

# RankedSource.relevance_score / credibility_score are 0-100 (see
# ranking_service.py) — a raw relevance_score below this is considered
# "low relevance" for the purposes of this evaluator only.
_LOW_RELEVANCE_THRESHOLD = 30.0
_LOW_COVERAGE_RATIO = 0.5


def evaluate_retrieval(
    query: str,
    requested_top_k: int,
    ranked_sources: Sequence,
) -> RetrievalEvaluation:
    """Aggregates the retrieval/ranking scores the pipeline already computed
    (RankedSource.relevance_score / credibility_score) rather than
    recomputing relevance from scratch — this reuses the existing ranking
    model instead of introducing a second, possibly-disagreeing one.
    """
    flags: list[EvalFlag] = []
    n = len(ranked_sources)
    coverage_ratio = round(n / requested_top_k, 4) if requested_top_k else 0.0

    if n == 0:
        flags.append(
            EvalFlag(
                code="no_sources",
                severity="critical",
                message="No sources were retrieved for this topic.",
            )
        )
        return RetrievalEvaluation(
            requested_top_k=requested_top_k,
            returned_source_count=0,
            coverage_ratio=0.0,
            score=0.0,
            flags=flags,
        )

    relevance_scores = [float(getattr(s, "relevance_score", 0.0)) for s in ranked_sources]
    credibility_scores = [float(getattr(s, "credibility_score", 0.0)) for s in ranked_sources]
    avg_relevance = round(sum(relevance_scores) / n, 2)
    avg_credibility = round(sum(credibility_scores) / n, 2)
    low_relevance_count = sum(1 for s in relevance_scores if s < _LOW_RELEVANCE_THRESHOLD)

    domains = {getattr(s, "domain", "") for s in ranked_sources if getattr(s, "domain", "")}
    domain_diversity_ratio = round(len(domains) / n, 4)

    if coverage_ratio < _LOW_COVERAGE_RATIO:
        flags.append(
            EvalFlag(
                code="low_coverage",
                severity="warning",
                message=(
                    f"Only {n} of {requested_top_k} requested sources were "
                    "returned — fewer unique/quality results were available."
                ),
            )
        )
    if low_relevance_count > n / 2:
        flags.append(
            EvalFlag(
                code="mostly_low_relevance",
                severity="warning",
                message="More than half of the retrieved sources scored low relevance to the topic.",
            )
        )
    if n >= 2 and len(domains) == 1:
        flags.append(
            EvalFlag(
                code="single_domain",
                severity="warning",
                message="All retrieved sources come from a single domain.",
            )
        )

    score = round(
        0.40 * min(coverage_ratio, 1.0)
        + 0.35 * (avg_relevance / 100.0)
        + 0.15 * (avg_credibility / 100.0)
        + 0.10 * min(domain_diversity_ratio * 2, 1.0),
        4,
    )

    return RetrievalEvaluation(
        requested_top_k=requested_top_k,
        returned_source_count=n,
        coverage_ratio=coverage_ratio,
        avg_relevance_score=avg_relevance,
        avg_credibility_score=avg_credibility,
        low_relevance_source_count=low_relevance_count,
        unique_domain_count=len(domains),
        domain_diversity_ratio=domain_diversity_ratio,
        score=score,
        flags=flags,
    )
