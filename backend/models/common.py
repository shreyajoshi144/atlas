from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class TimestampedSchema(BaseSchema):
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SourceType(str, Enum):
    SEARCH_RESULT = "search_result"
    WEB_PAGE = "web_page"
    REPORT = "report"
    SUMMARY = "summary"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    NOT_VERIFIED = "not_verified"
    CONFLICTING = "conflicting"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ResearchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EventType(str, Enum):
    RESEARCH_STARTED = "research_started"
    SEARCH_COMPLETED = "search_completed"
    RANKING_COMPLETED = "ranking_completed"
    SCRAPING_COMPLETED = "scraping_completed"
    SYNTHESIS_COMPLETED = "synthesis_completed"
    REPORT_GENERATED = "report_generated"
    VERIFICATION_COMPLETED = "verification_completed"
    MEMORY_SAVED = "memory_saved"
    CHAT_COMPLETED = "chat_completed"
    ERROR = "error"


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MetricBreakdown(BaseSchema):
    retrieval_latency_ms: int = 0
    scraping_latency_ms: int = 0
    llm_latency_ms: int = 0
    total_execution_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ErrorInfo(BaseSchema):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None