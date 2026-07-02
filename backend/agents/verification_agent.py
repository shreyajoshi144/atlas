from __future__ import annotations
from typing import List
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from backend.core.llm import get_chat_llm
from backend.core.logging import get_logger
from backend.models.common import VerificationStatus
from backend.models.domain_models import ClaimEvidence, VerificationSummary, VerifiedClaim

logger = get_logger(__name__)

class VerificationEvidenceOutput(BaseModel):
    url: str
    title: str
    evidence_text: str
    evidence_strength: float = Field(..., ge=0.0, le=1.0)


class VerificationClaimOutput(BaseModel):
    claim_text: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    status: VerificationStatus
    evidence: List[VerificationEvidenceOutput] = Field(default_factory=list)


class VerificationOutput(BaseModel):
    overall_verification_score: float = Field(..., ge=0.0, le=100.0)
    verdict: str
    claims: List[VerificationClaimOutput] = Field(default_factory=list)


class VerificationAgent:
    def __init__(self) -> None:
        self.llm = get_chat_llm().with_structured_output(VerificationOutput)

    def verify(
        self,
        report_markdown: str,
        scraped_research_text: str,
    ) -> VerificationSummary:
        system_prompt = (
            "You are a research verification specialist. "
            "Extract factual claims from the report, compare them against the provided evidence, "
            "and return a structured verification result. "
            "Do not verify one claim at a time; handle the whole report in one pass. "
            "Be conservative with confidence scores."
        )

        human_prompt = f"""
Report to verify:
{report_markdown}

Evidence corpus:
{scraped_research_text}

Instructions:
1. Extract the most important factual claims from the report.
2. Match each claim to evidence from the corpus.
3. Mark each claim as verified, partially_verified, not_verified, conflicting, or insufficient_evidence.
4. Provide confidence between 0 and 1.
5. Return overall verification score between 0 and 100.
6. Keep evidence snippets short and grounded in the corpus.
"""

        logger.info("Starting verification")
        output = self.llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ]
        )

        verified_claims: list[VerifiedClaim] = []
        for claim in output.claims:
            evidence_items = [
                ClaimEvidence(
                    source_id="",
                    url=str(item.url),
                    title=item.title,
                    evidence_text=item.evidence_text,
                    evidence_strength=item.evidence_strength,
                )
                for item in claim.evidence
            ]
            verified_claims.append(
                VerifiedClaim(
                    claim_text=claim.claim_text,
                    confidence=claim.confidence,
                    status=claim.status,
                    evidence=evidence_items,
                )
            )

        summary = VerificationSummary(
            overall_verification_score=output.overall_verification_score,
            verdict=output.verdict,
            claims=verified_claims,
        )

        logger.info(
            "Verification complete",
            extra={
                "overall_verification_score": summary.overall_verification_score,
                "claim_count": len(summary.claims),
            },
        )
        return summary