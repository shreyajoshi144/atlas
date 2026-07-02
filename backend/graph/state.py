from __future__ import annotations

from typing import Any, Optional, TypedDict

from backend.models.common import MetricBreakdown, ResearchStatus
from backend.models.domain_models import (
    ExecutiveBrief,
    RankedSource,
    ResearchReport,
    ResearchSession,
    ScrapedContent,
    SearchResult,
    VerificationSummary,
)

class ResearchGraphState(TypedDict, total=False):
    topic: str
    user_context: Optional[str]

    session: ResearchSession
    status: ResearchStatus
    metrics: MetricBreakdown

    search_results: list[SearchResult]
    ranked_sources: list[RankedSource]
    scraped_contents: list[ScrapedContent]

    evidence_text: str
    verification_summary: VerificationSummary

    generated_summary: str
    generated_report_markdown: str
    key_statistics: list[str]
    executive_brief: ExecutiveBrief
    final_report: ResearchReport

    include_verification: bool
    include_executive_brief: bool
    persist_memory: bool

    errors: list[str]
    debug: dict[str, Any]