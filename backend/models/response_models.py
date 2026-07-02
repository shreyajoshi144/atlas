from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

# ── Generic wrappers ──────────────────────────────────────────────────────────

class ApiResponse(BaseModel, Generic[T]):
    success: bool
    message: str
    data: T | None = None


class ErrorResponse(BaseModel):
    detail: str

# ── Source ────────────────────────────────────────────────────────────────────

class SourceResponse(BaseModel):
    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    publish_date: str | None = None
    relevance_score: float
    credibility_score: float
    final_score: float = 0.0
    rank_position: int

# ── Verification ──────────────────────────────────────────────────────────────

class VerifiedClaimResponse(BaseModel):
    claim_text: str
    evidence: list[Any] = []
    confidence: float
    status: str  # "verified" | "partially_verified" | "not_verified"

class VerificationSummaryResponse(BaseModel):
    claims: list[VerifiedClaimResponse] = []
    overall_verification_score: float
    verdict: str

# ── Executive brief ───────────────────────────────────────────────────────────

class ExecutiveBriefResponse(BaseModel):
    executive_summary: str
    key_insights: list[str] = []
    risks: list[str] = []
    opportunities: list[str] = []
    recommended_actions: list[str] = []

# ── Session ───────────────────────────────────────────────────────────────────

class ResearchSessionResponse(BaseModel):
    session_id: str
    topic: str
    status: str
    report_id: str | None = None
    created_at: datetime
    updated_at: datetime
    retrieval_latency_ms: int = 0
    scraping_latency_ms: int = 0
    llm_latency_ms: int = 0
    total_execution_ms: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

# ── Report ────────────────────────────────────────────────────────────────────

class ResearchReportResponse(BaseModel):
    report_id: str
    session_id: str
    topic_id: str
    topic: str
    summary: str
    report_markdown: str
    key_statistics: list[str] = []
    swot: dict[str, list[str]] = {}
    verification: VerificationSummaryResponse | None = None
    executive_brief: ExecutiveBriefResponse | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

# ── Research run ──────────────────────────────────────────────────────────────

class ResearchRunResponse(BaseModel):
    session: ResearchSessionResponse
    report: ResearchReportResponse
    top_sources: list[SourceResponse]
    verification: VerificationSummaryResponse | None = None

# ── Semantic search ───────────────────────────────────────────────────────────

class SemanticSearchResultResponse(BaseModel):
    report_id: str
    topic: str
    summary: str
    similarity_score: float


class SemanticSearchResponse(BaseModel):
    results: list[SemanticSearchResultResponse]

# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatMessageResponse(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class RetrievedChunkResponse(BaseModel):
    report_id: str
    chunk_id: str
    session_id: str
    topic: str
    chunk_index: int
    content: str
    similarity_score: float

class ChatResponse(BaseModel):
    """
    Returned at data level from POST /api/chat/research.
    Frontend reads: data.answer, data.messages, data.retrieved_chunks
    """
    answer: str
    messages: list[ChatMessageResponse]
    retrieved_chunks: list[RetrievedChunkResponse]
    report_ids: list[str] = []

# ── History ───────────────────────────────────────────────────────────────────

class HistoryReportItemResponse(BaseModel):
    report_id: str
    session_id: str
    topic_id: str
    topic: str
    summary: str | None = None
    verification_score: float | None = None
    verification_verdict: str | None = None
    created_at: datetime
    updated_at: datetime


class HistoryReportsResponse(BaseModel):
    reports: list[HistoryReportItemResponse]
    total: int
    limit: int
    offset: int


class ReportDetailResponse(BaseModel):
    report: ResearchReportResponse
    sources: list[SourceResponse]

# ── Analytics ─────────────────────────────────────────────────────────────────

class AnalyticsOverviewResponse(BaseModel):
    total_reports_generated: int = 0
    total_sources_scraped: int = 0
    average_verification_score: float = 0.0
    average_execution_time_ms: float = 0.0
    total_tokens_used: int = 0
    total_estimated_cost_usd: float = 0.0
    average_retrieval_latency_ms: float = 0.0
    reports_generated_over_time: list[dict] = []
    verification_score_trend: list[dict] = []
    top_topics: list[dict] = []
    source_domain_distribution: list[dict] = []

# ── Knowledge base ────────────────────────────────────────────────────────────

class KnowledgeBaseResponse(BaseModel):
    total_reports: int = 0
    total_chunks: int = 0
    chroma_collection_size: int = 0
    recent_topics: list[str] = []
    report_ids: list[str] = []

# Alias for backward-compatible imports
ResearchChatResponse = ChatResponse