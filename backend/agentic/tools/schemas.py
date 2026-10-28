"""Phase 1B — validated input schemas and typed outputs for the five tools.

All models inherit ``BaseSchema`` (``extra="forbid"``): an agent cannot pass
a parameter a tool does not declare. Outputs re-use existing domain models
(``RankedSource``, ``ScrapedContent``, ``VerificationSummary``) rather than
redefining them.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, HttpUrl, field_validator

from backend.agentic.common import MAX_QUERY_CHARS, ResearchMode
from backend.models.common import BaseSchema
from backend.models.domain_models import RankedSource, ScrapedContent, VerificationSummary


def _non_blank(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


# ── search_web ────────────────────────────────────────────────────────────────
class SearchWebInput(BaseSchema):
    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    max_results: int = Field(default=10, ge=1, le=20)
    research_mode: ResearchMode = Field(default=ResearchMode.EXPLORE, validate_default=True)

    _clean_query = field_validator("query")(_non_blank)


class SearchWebOutput(BaseSchema):
    query: str
    research_mode: ResearchMode
    raw_result_count: int = Field(ge=0)
    sources: list[RankedSource]


# ── scrape_source ─────────────────────────────────────────────────────────────
class ScrapeSourceInput(BaseSchema):
    url: HttpUrl
    title: Optional[str] = Field(default=None, max_length=300)
    source_id: Optional[str] = Field(default=None, min_length=1, max_length=120)


class ScrapeSourceOutput(BaseSchema):
    source_id: str
    success: bool
    scraped: Optional[ScrapedContent] = None
    error_message: Optional[str] = None


# ── search_existing_evidence ──────────────────────────────────────────────────
class SearchExistingEvidenceInput(BaseSchema):
    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    # Mandatory tenant scope. It must be supplied by the trusted caller (the
    # authenticated session), never by model-generated text.
    user_id: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=5, ge=1, le=10)
    topic: Optional[str] = Field(default=None, min_length=1, max_length=300)

    _clean_query = field_validator("query")(_non_blank)


class ExistingEvidenceHit(BaseSchema):
    report_id: str
    topic: str
    summary: str
    similarity_score: float = Field(ge=0.0, le=1.0)


class SearchExistingEvidenceOutput(BaseSchema):
    query: str
    hits: list[ExistingEvidenceHit]


# ── verify_claim ──────────────────────────────────────────────────────────────
class VerifyClaimInput(BaseSchema):
    claim: str = Field(min_length=10, max_length=1000)
    evidence_text: str = Field(min_length=1, max_length=200_000)

    _clean_claim = field_validator("claim")(_non_blank)
    _clean_evidence = field_validator("evidence_text")(_non_blank)


class VerificationAssessment(BaseSchema):
    """Normalized verification decision used by the bounded re-planner."""

    status: Literal[
        "supported",
        "insufficient_evidence",
        "conflicting_evidence",
        "unsupported_claim",
    ]
    rationale: str = Field(min_length=1, max_length=1000)


class EvidenceGap(BaseSchema):
    """Minimal structured information required to target one follow-up search."""

    claim: str = Field(min_length=1, max_length=1000)
    missing_evidence: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)
    preferred_tool: Optional[str] = Field(default=None, max_length=80)
    preferred_source: Optional[str] = Field(default=None, max_length=500)


class VerifyClaimOutput(BaseSchema):
    claim: str
    verification: VerificationSummary
    assessment: Optional[VerificationAssessment] = None
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list, max_length=20)


# ── request_more_evidence ─────────────────────────────────────────────────────
class RequestMoreEvidenceInput(BaseSchema):
    question: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    gap_description: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    exclude_urls: list[HttpUrl] = Field(default_factory=list, max_length=100)
    max_results: int = Field(default=5, ge=1, le=10)
    research_mode: ResearchMode = Field(default=ResearchMode.EXPLORE, validate_default=True)

    _clean_question = field_validator("question")(_non_blank)
    _clean_gap = field_validator("gap_description")(_non_blank)


class RequestMoreEvidenceOutput(BaseSchema):
    query: str
    research_mode: ResearchMode
    filtered_already_seen: int = Field(ge=0)
    new_sources: list[RankedSource]
