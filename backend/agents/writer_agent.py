from __future__ import annotations
from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from backend.core.llm import get_chat_llm
from backend.core.logging import get_logger

logger = get_logger(__name__)

class WriterOutput(BaseModel):
    summary: str = Field(..., min_length=20)
    report_markdown: str = Field(..., min_length=100)
    key_statistics: list[str] = Field(default_factory=list)


class WriterAgent:
    def __init__(self) -> None:
        self.llm = get_chat_llm().with_structured_output(WriterOutput)

    def generate_report(
        self,
        topic: str,
        research_text: str,
    ) -> WriterOutput:
        system_prompt = (
            "You are a senior AI research analyst. "
            "Generate a high-quality, portfolio-grade research report in structured form. "
            "Be precise, factual, and concise. "
            "Do not fabricate statistics. "
            "If the evidence is mixed or limited, state that clearly."
        )

        human_prompt = f"""
Topic:
{topic}

Research Evidence:
{research_text}

Return JSON matching the schema with:
1. A concise summary.
2. A detailed markdown report.
3. A list of key statistics or quantified facts explicitly supported by the evidence.

The markdown report must include:
- Introduction
- Key Findings
- Risks
- Opportunities
- Conclusion
- Sources

Rules:
- Use only the provided evidence.
- Mention uncertainty where needed.
- Do not include fake citations.
- Keep the writing professional and recruiter-friendly.
"""

        logger.info("Generating structured report", extra={"topic": topic})
        result = self.llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ]
        )
        logger.info(
            "Structured report generated",
            extra={
                "topic": topic,
                "summary_length": len(result.summary),
                "report_length": len(result.report_markdown),
            },
        )
        return result