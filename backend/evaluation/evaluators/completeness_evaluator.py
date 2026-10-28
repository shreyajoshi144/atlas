from __future__ import annotations

from backend.evaluation.models import CompletenessEvaluation, EvalFlag
from backend.evaluation.text_utils import containment_ratio

_THRESHOLD = 0.45


def evaluate_completeness(*, research_plan, evidence_text: str, report_markdown: str) -> CompletenessEvaluation:
    """Measure coverage of explicit plan requirements in evidence/report.

    Each required-evidence description is checked independently. Source count
    is deliberately not a scoring input; sources only matter insofar as their
    evidence is represented in the evidence/report material.
    """
    requirements = list(getattr(research_plan, "required_evidence", None) or []) if research_plan else []
    if not requirements:
        return CompletenessEvaluation(score=None, requirement_count=0, satisfied_requirement_count=0,
                                      requirement_coverage=None,
                                      flags=[EvalFlag(code="no_requirements", severity="info", message="No structured research requirements were available.")])
    corpus = f"{evidence_text or ''}\n\n{report_markdown or ''}"
    satisfied = 0
    details = []
    for req in requirements:
        ratio = round(containment_ratio(req.description, corpus), 4)
        ok = ratio >= _THRESHOLD
        satisfied += int(ok)
        details.append({"requirement_id": req.requirement_id, "description": req.description,
                        "evidence_type": str(req.evidence_type), "match_ratio": ratio, "satisfied": ok})
    coverage = round(satisfied / len(requirements), 4)
    flags = []
    if coverage < 1.0:
        flags.append(EvalFlag(code="incomplete_research", severity="warning",
                              message=f"{len(requirements) - satisfied} of {len(requirements)} planned evidence requirements were not sufficiently represented in the evidence/report."))
    return CompletenessEvaluation(requirement_count=len(requirements), satisfied_requirement_count=satisfied,
                                  requirement_coverage=coverage, score=coverage, requirement_details=details, flags=flags)
