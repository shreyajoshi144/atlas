from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

Severity = Literal["info", "warning", "critical"]


class EvalFlag(BaseModel):
    code: str
    severity: Severity = "info"
    message: str


class RetrievalEvaluation(BaseModel):
    requested_top_k: int = 0
    returned_source_count: int = 0
    coverage_ratio: float = 0.0
    avg_relevance_score: float = 0.0
    avg_credibility_score: float = 0.0
    low_relevance_source_count: int = 0
    unique_domain_count: int = 0
    domain_diversity_ratio: float = 0.0
    score: float = 0.0
    flags: list[EvalFlag] = Field(default_factory=list)


class ClaimGroundingDetail(BaseModel):
    claim_id: str
    claim_text: str
    status: str
    confidence: float
    evidence_count: int
    grounded_evidence_count: int
    unmatched_source_count: int
    avg_evidence_overlap: float
    is_grounded: bool


class GroundingEvaluation(BaseModel):
    claim_count: int = 0
    grounded_claim_count: int = 0
    grounding_rate: float = 0.0
    avg_evidence_overlap: float = 0.0
    unmatched_source_citations: int = 0
    score: float = 0.0
    claim_details: list[ClaimGroundingDetail] = Field(default_factory=list)
    flags: list[EvalFlag] = Field(default_factory=list)


class VerificationQualityEvaluation(BaseModel):
    overall_verification_score: float | None = None
    claim_count: int = 0
    status_distribution: dict[str, int] = Field(default_factory=dict)
    confidence_status_mismatches: int = 0
    invalid_stance_count: int = 0
    empty_evidence_verified_count: int = 0
    score: float = 0.0
    flags: list[EvalFlag] = Field(default_factory=list)


class CitationCorrectnessEvaluation(BaseModel):
    cited_url_count: int = 0
    valid_citation_count: int = 0
    invalid_citation_count: int = 0
    citation_precision: float | None = None
    score: float = 0.0
    flags: list[EvalFlag] = Field(default_factory=list)


class AnswerGroundednessEvaluation(BaseModel):
    report_length_chars: int = 0
    overlap_with_evidence: float = 0.0
    cited_url_count: int = 0
    valid_citation_count: int = 0
    invalid_citation_count: int = 0
    citation_precision: float | None = None
    key_statistics_supported_ratio: float = 1.0
    score: float = 0.0
    flags: list[EvalFlag] = Field(default_factory=list)


class CompletenessEvaluation(BaseModel):
    requirement_count: int = 0
    satisfied_requirement_count: int = 0
    requirement_coverage: float | None = None
    requirement_details: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None
    flags: list[EvalFlag] = Field(default_factory=list)


class AgentQualityEvaluation(BaseModel):
    plan_completion: float | None = None
    initial_plan_completion: float | None = None
    adaptive_recovery_completion: float | None = None
    tool_selection_validity: float | None = None
    unnecessary_tool_calls: int = 0
    tool_efficiency: float | None = None
    replan_count: int = 0
    replan_effectiveness: float | None = None
    successful_recoveries: int = 0
    appropriate_stopping: float | None = None
    duplicate_research_actions: int = 0
    duplicate_research_rate: float = 0.0
    score: float | None = None
    flags: list[EvalFlag] = Field(default_factory=list)

    def with_composite(self) -> "AgentQualityEvaluation":
        # Intentionally no aggregate agent-quality score: the component
        # metrics have different operational meanings and should not be
        # collapsed into an arbitrary number.
        return self


class SystemPerformanceEvaluation(BaseModel):
    total_latency_ms: int | None = None
    node_latency_ms: dict[str, int] = Field(default_factory=dict)
    per_tool_latency_ms: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_count: int | None = None
    replan_count: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None


class ResearchQualityEvaluation(BaseModel):
    retrieval: RetrievalEvaluation | None = None
    grounding: GroundingEvaluation | None = None
    citation_correctness: CitationCorrectnessEvaluation | None = None
    verification: VerificationQualityEvaluation | None = None
    completeness: CompletenessEvaluation | None = None
    answer_groundedness: AnswerGroundednessEvaluation | None = None
    score: float | None = None


class EvaluationSummary(BaseModel):
    """Deterministic evaluation of one research run.

    ``overall_score`` is explicitly a composite heuristic, not factual accuracy.
    Component metrics remain independently visible and are the preferred basis
    for regression decisions.
    """
    schema_version: str = "2.0"
    report_id: str | None = None
    session_id: str | None = None
    topic: str = ""

    retrieval: RetrievalEvaluation | None = None
    grounding: GroundingEvaluation | None = None
    citation_correctness: CitationCorrectnessEvaluation | None = None
    verification_quality: VerificationQualityEvaluation | None = None
    answer_groundedness: AnswerGroundednessEvaluation | None = None
    completeness: CompletenessEvaluation | None = None
    research_quality: ResearchQualityEvaluation | None = None
    agent_quality: AgentQualityEvaluation | None = None
    performance: SystemPerformanceEvaluation | None = None

    overall_score: float | None = None
    flags: list[EvalFlag] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def model_dump_compact(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
