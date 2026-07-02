from __future__ import annotations
from datetime import date, datetime
from typing import Any, Optional
from pydantic import Field, HttpUrl, field_validator
from backend.models.common import (
    BaseSchema,
    ChatRole,
    EventType,
    MetricBreakdown,
    ResearchStatus,
    SourceType,
    TimestampedSchema,
    VerificationStatus,
    generate_id,
)

class SearchResult(BaseSchema):
    title: str
    url: HttpUrl
    snippet: str
    domain: str
    rank_position: int
    source_type: SourceType = SourceType.SEARCH_RESULT


class RankedSource(BaseSchema):
    source_id: str = Field(default_factory=lambda: generate_id("src"))
    title: str
    url: HttpUrl
    domain: str
    snippet: str = ""
    publish_date: Optional[date] = None
    bm25_score: float = 0.0
    tfidf_score: float = 0.0
    cosine_score: float = 0.0
    fuzz_score: float = 0.0
    relevance_score: float = 0.0
    credibility_score: float = 0.0
    rank_position: int = 0
    source_type: SourceType = SourceType.WEB_PAGE

    @field_validator(
        "bm25_score",
        "tfidf_score",
        "cosine_score",
        "fuzz_score",
        "relevance_score",
        "credibility_score",
    )
    @classmethod
    def clamp_score(cls, value: float) -> float:
        return round(max(0.0, value), 4)


class ScrapedContent(BaseSchema):
    source_id: str
    url: HttpUrl
    title: str
    domain: str
    publish_date: Optional[date] = None
    content: str
    content_hash: str
    success: bool = True
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    content_length: int = 0
    retrieved_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("content_length", mode="before")
    @classmethod
    def derive_content_length(cls, value: int | None) -> int:
        return value or 0


class ClaimEvidence(BaseSchema):
    source_id: str
    url: HttpUrl
    title: str
    evidence_text: str
    evidence_strength: float = 0.0

    @field_validator("evidence_strength")
    @classmethod
    def normalize_strength(cls, value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 4)


class VerifiedClaim(BaseSchema):
    claim_id: str = Field(default_factory=lambda: generate_id("claim"))
    claim_text: str
    evidence: list[ClaimEvidence] = Field(default_factory=list)
    confidence: float = 0.0
    status: VerificationStatus = VerificationStatus.INSUFFICIENT_EVIDENCE

    @field_validator("confidence")
    @classmethod
    def normalize_confidence(cls, value: float) -> float:
        return round(min(max(value, 0.0), 1.0), 4)


class VerificationSummary(BaseSchema):
    overall_verification_score: float = 0.0
    verdict: str
    claims: list[VerifiedClaim] = Field(default_factory=list)

    @field_validator("overall_verification_score")
    @classmethod
    def normalize_score(cls, value: float) -> float:
        return round(min(max(value, 0.0), 100.0), 2)


class ExecutiveBrief(BaseSchema):
    executive_summary: str
    key_insights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class ResearchReport(BaseSchema):
    report_id: str = Field(default_factory=lambda: generate_id("rpt"))
    topic: str
    summary: str
    report_markdown: str
    key_statistics: list[str] = Field(default_factory=list)
    swot: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "strengths": [],
            "weaknesses": [],
            "opportunities": [],
            "threats": [],
        }
    )
    sources: list[RankedSource] = Field(default_factory=list)
    verification: Optional[VerificationSummary] = None
    executive_brief: Optional[ExecutiveBrief] = None


class ResearchSession(TimestampedSchema):
    session_id: str = Field(default_factory=lambda: generate_id("sess"))
    topic: str
    status: ResearchStatus = ResearchStatus.PENDING
    report_id: Optional[str] = None
    query_text: Optional[str] = None
    metrics: MetricBreakdown = Field(default_factory=MetricBreakdown)
    error_message: Optional[str] = None


class AnalyticsEvent(TimestampedSchema):
    event_id: str = Field(default_factory=lambda: generate_id("evt"))
    session_id: str
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseSchema):
    message_id: str = Field(default_factory=lambda: generate_id("msg"))
    role: ChatRole
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ChatTurn(BaseSchema):
    topic: str
    session_id: Optional[str] = None
    messages: list[ChatMessage] = Field(default_factory=list)
    retrieved_report_ids: list[str] = Field(default_factory=list)
    answer: Optional[str] = None