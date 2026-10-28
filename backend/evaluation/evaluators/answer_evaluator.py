from __future__ import annotations

from typing import Sequence

from backend.evaluation.models import AnswerGroundednessEvaluation, EvalFlag
from backend.evaluation.text_utils import cosine_similarity, extract_urls, normalize_url

_LOW_OVERLAP_THRESHOLD = 0.15
_STAT_SUPPORT_THRESHOLD = 0.20
_LOW_STAT_SUPPORT_RATIO = 0.5


def evaluate_answer_groundedness(
    report_markdown: str,
    key_statistics: Sequence[str],
    ranked_sources: Sequence,
    evidence_text: str,
) -> AnswerGroundednessEvaluation:
    """Checks the final written report against the evidence it was supposed
    to be built from, plus citation correctness: any http(s) URL that
    appears in the report but does not match a retrieved source is flagged
    as a likely fabricated/incorrect citation.
    """
    flags: list[EvalFlag] = []
    report_markdown = report_markdown or ""

    if not report_markdown.strip():
        flags.append(
            EvalFlag(code="empty_report", severity="critical", message="Report markdown is empty.")
        )
        return AnswerGroundednessEvaluation(score=0.0, flags=flags)

    overlap = round(cosine_similarity(report_markdown, evidence_text or ""), 4)

    known_urls = {normalize_url(str(getattr(s, "url", ""))) for s in ranked_sources}
    cited_urls = [normalize_url(u) for u in extract_urls(report_markdown)]
    valid_count = sum(1 for u in cited_urls if u in known_urls)
    invalid_count = len(cited_urls) - valid_count
    citation_precision = round(valid_count / len(cited_urls), 4) if cited_urls else None

    if invalid_count > 0:
        flags.append(
            EvalFlag(
                code="uncited_or_fabricated_url",
                severity="critical",
                message=f"{invalid_count} URL(s) in the report do not match any retrieved source.",
            )
        )
    if overlap < _LOW_OVERLAP_THRESHOLD:
        flags.append(
            EvalFlag(
                code="low_evidence_overlap",
                severity="warning",
                message="The report has low lexical overlap with the collected evidence — it may not be well grounded.",
            )
        )

    supported = 0
    stats = list(key_statistics or [])
    for stat in stats:
        if cosine_similarity(stat, evidence_text or "") >= _STAT_SUPPORT_THRESHOLD:
            supported += 1
    stat_ratio = round(supported / len(stats), 4) if stats else 1.0
    if stats and stat_ratio < _LOW_STAT_SUPPORT_RATIO:
        flags.append(
            EvalFlag(
                code="unsupported_statistics",
                severity="warning",
                message="Fewer than half of the reported key statistics could be matched to the evidence.",
            )
        )

    # Citation precision only penalizes the score when URLs were actually
    # cited and some were invalid; reports that cite no raw URLs (this
    # writer prompt doesn't require inline links) are treated as neutral
    # on this component rather than penalized for an absent behavior.
    citation_component = citation_precision if citation_precision is not None else 1.0

    score = round(
        0.40 * min(overlap * 2.5, 1.0) + 0.30 * citation_component + 0.30 * stat_ratio,
        4,
    )

    return AnswerGroundednessEvaluation(
        report_length_chars=len(report_markdown),
        overlap_with_evidence=overlap,
        cited_url_count=len(cited_urls),
        valid_citation_count=valid_count,
        invalid_citation_count=invalid_count,
        citation_precision=citation_precision,
        key_statistics_supported_ratio=stat_ratio,
        score=score,
        flags=flags,
    )
