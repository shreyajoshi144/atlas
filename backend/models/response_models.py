from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

# Reused directly as the API's evaluation schema (see EvaluationSummary in
# backend/evaluation/models.py) rather than duplicating an equivalent set of
# response classes — the evaluation layer's output is already a plain,
# JSON-safe pydantic model with no dependency on the graph/agents packages.
from backend.evaluation.models import EvaluationSummary as EvaluationSummaryResponse

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

class EvidenceBucketResponse(BaseModel):
    count: int = 0
    source_ids: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)

class EvidenceConclusionResponse(BaseModel):
    supported_by_source_count: int = 0
    partially_supported_source_count: int = 0
    contradicted_source_count: int = 0

class ResearchInsightsResponse(BaseModel):
    supporting: EvidenceBucketResponse
    partially_supported: EvidenceBucketResponse
    contradicted: EvidenceBucketResponse
    mixed: EvidenceBucketResponse
    gaps: list[dict[str, str]] = Field(default_factory=list)
    conclusion: EvidenceConclusionResponse

# ── Research run ──────────────────────────────────────────────────────────────

class ResearchRunResponse(BaseModel):
    insights: ResearchInsightsResponse | None = None
    session: ResearchSessionResponse
    report: ResearchReportResponse
    top_sources: list[SourceResponse]
    verification: VerificationSummaryResponse | None = None
    # Echoes request.source_report_id back once ownership has been
    # verified server-side — None if it wasn't provided, or if it didn't
    # actually belong to the requesting user (silently dropped, not
    # errored, since it's provenance metadata, not a required input).
    source_report_id: str | None = None
    verification_status: Literal["succeeded", "failed", "unavailable"] = "unavailable"
    # Non-blocking pipeline evaluation (retrieval relevance, claim/evidence
    # grounding, verification consistency, final-answer groundedness and
    # citation correctness). None if evaluation could not be computed at
    # all — this never indicates the research run itself failed.
    evaluation: EvaluationSummaryResponse | None = None
    # Additive observability for the controlled 1C–1E path; existing clients may ignore these fields.
    agentic_status: str | None = None
    agentic_verification_status: str | None = None
    replan_count: int = 0
    agentic_trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    research_trace: list[dict[str, Any]] = Field(default_factory=list)

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
    Frontend reads: data.answer, data.messages, data.retrieved_chunks, data.suggested_questions
    """
    answer: str
    messages: list[ChatMessageResponse]
    retrieved_chunks: list[RetrievedChunkResponse]
    report_ids: list[str] = []
    suggested_questions: list[str] = []


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
    evaluation: EvaluationSummaryResponse | None = None
    # Additive observability for the controlled 1C–1E path; existing clients may ignore these fields.
    agentic_status: str | None = None
    agentic_verification_status: str | None = None
    replan_count: int = 0
    agentic_trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    research_trace: list[dict[str, Any]] = Field(default_factory=list)

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

# ── Auth ──────────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    user_id: str
    email: str
    name: str
    created_at: datetime


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class ForgotPasswordResponse(BaseModel):
    # Populated only outside production, since no email provider is wired
    # up yet — see backend/api/routes/auth.py. Always None in production;
    # never logged (see the "safe logging" hardening in that same route).
    dev_reset_token: str | None = None


# ── Unanswered questions ─────────────────────────────────────────────────
# Derived (never fabricated) from a report's existing verification claims
# and sources — see backend/services/questions_service.py.
class UnansweredQuestionResponse(BaseModel):
    question_id: str
    question_text: str
    category: str
    rationale: str
    related_claim_id: str | None = None
    related_source_ids: list[str] = Field(default_factory=list)


class UnansweredQuestionsResponse(BaseModel):
    report_id: str
    topic: str
    questions: list[UnansweredQuestionResponse]


# ── Notes ─────────────────────────────────────────────────────────────────────
# Plain, persistent student notes — one per (user, report). Deliberately
# separate from ExecutiveBriefResponse/ResearchReportResponse above: those are
# Atlas-generated; this is only ever what the student typed.

class NoteResponse(BaseModel):
    report_id: str
    content: str
    updated_at: datetime | None = None