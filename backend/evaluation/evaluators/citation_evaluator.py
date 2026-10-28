from __future__ import annotations

from backend.evaluation.models import CitationCorrectnessEvaluation, EvalFlag
from backend.evaluation.text_utils import extract_urls, normalize_url


def evaluate_citation_correctness(report_markdown: str, ranked_sources) -> CitationCorrectnessEvaluation:
    cited = list(dict.fromkeys(extract_urls(report_markdown or "")))
    known = {normalize_url(str(getattr(s, "url", ""))) for s in (ranked_sources or [])}
    valid = sum(1 for url in cited if normalize_url(url) in known)
    invalid = len(cited) - valid
    precision = round(valid / len(cited), 4) if cited else None
    flags = []
    if invalid:
        flags.append(EvalFlag(code="citation_mismatch", severity="critical",
                              message=f"{invalid} report citation(s) do not match retrieved sources."))
    if not cited:
        flags.append(EvalFlag(code="no_citations", severity="warning", message="The final report contains no extractable source URLs."))
    score = precision if precision is not None else 0.0
    return CitationCorrectnessEvaluation(cited_url_count=len(cited), valid_citation_count=valid,
                                         invalid_citation_count=invalid, citation_precision=precision,
                                         score=round(score, 4), flags=flags)
