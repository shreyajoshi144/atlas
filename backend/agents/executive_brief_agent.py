from __future__ import annotations
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from backend.core.llm import get_chat_llm
from backend.core.logging import get_logger
from backend.models.domain_models import ExecutiveBrief

logger = get_logger(__name__)

class ExecutiveBriefOutput(BaseModel):
    executive_summary: str
    key_insights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)

class ExecutiveBriefAgent:
    def __init__(self) -> None:
        self.llm = get_chat_llm().with_structured_output(ExecutiveBriefOutput)

    def generate(
        self,
        topic: str,
        report_markdown: str,
        verification_score: float | None = None,
    ) -> ExecutiveBrief:
        system_prompt = (
            "You are a strategic research analyst writing concise executive briefs for technical and business audiences. "
            "Focus on decisions, risks, opportunities, and practical next steps."
        )

        human_prompt = f"""
Topic:
{topic}

Research report:
{report_markdown}

Verification score:
{verification_score if verification_score is not None else "Not available"}

Return a structured executive brief with:
- executive_summary
- key_insights
- risks
- opportunities
- recommended_actions

Rules:
- Be direct and business-oriented.
- Do not repeat the full report.
- Base all recommendations on the report content.
"""

        logger.info("Generating executive brief", extra={"topic": topic})
        output = self.llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ]
        )

        brief = ExecutiveBrief(
            executive_summary=output.executive_summary,
            key_insights=output.key_insights,
            risks=output.risks,
            opportunities=output.opportunities,
            recommended_actions=output.recommended_actions,
        )

        logger.info(
            "Executive brief generated",
            extra={"topic": topic, "insight_count": len(brief.key_insights)},
        )
        return brief