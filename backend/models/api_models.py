from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChatMessageRequest(BaseModel):
    role: Literal["user", "assistant"] = Field(..., description="Message sender role")
    content: str = Field(..., min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be empty")
        return cleaned


class ResearchRunRequest(BaseModel):
    research_mode: Literal["explore", "compare", "investigate", "literature_review", "brief"] = "explore"
    topic: str = Field(..., min_length=3, max_length=300)
    user_context: str | None = Field(default=None, max_length=3000)
    include_verification: bool = True
    include_executive_brief: bool = True
    persist_memory: bool = True
    # Was capped at 10, which made the product's documented "15-20 sources
    # for deeper research" mode impossible to request through this API —
    # the pipeline itself already supports any top_k (retrieve_and_rank()
    # fetches a larger pool and ranks/slices to top_k), so this was purely
    # a validation-layer gap. See Sprint 1A report for the full rationale.
    top_k_urls: int | None = Field(default=None, ge=1, le=20)
    # Optional provenance for "Research this question": set when this run
    # was triggered by selecting a derived unanswered question from an
    # earlier report, so the two can be linked without a new pipeline or
    # a new persistence mechanism. Ownership of source_report_id is
    # re-checked server-side (see research.py) — never trusted as-is.
    source_report_id: str | None = Field(default=None, max_length=120)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("topic must not be empty")
        return cleaned


class QuestionResearchRequest(BaseModel):
    question_text: str = Field(..., min_length=3, max_length=2000)

    @field_validator("question_text")
    @classmethod
    def validate_question_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question_text must not be empty")
        return cleaned


class ResearchChatRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)
    question: str = Field(..., min_length=3, max_length=2000)
    session_id: str | None = Field(default=None, max_length=120)
    report_id: str | None = Field(default=None, max_length=120)
    history: list[ChatMessageRequest] = Field(default_factory=list, max_length=40)
    top_k_chunks: int = Field(default=4, ge=1, le=10)

    @field_validator("topic", "question")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be empty")
        return cleaned


class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)
    topic: str | None = Field(default=None, max_length=300)
    report_id: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query must not be empty")
        return cleaned

class SettingsUpdateRequest(BaseModel):
    openai_model: str | None = Field(default=None, max_length=100)
    tavily_max_results: int | None = Field(default=None, ge=1, le=20)
    default_top_k_urls: int | None = Field(default=None, ge=1, le=20)
    scrape_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    scrape_max_retries: int | None = Field(default=None, ge=0, le=5)


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    name: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if "@" not in cleaned or "." not in cleaned.split("@")[-1]:
            raise ValueError("must be a valid email address")
        return cleaned

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        return cleaned


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return value.strip().lower()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return value.strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=8, max_length=128)


class NoteUpdateRequest(BaseModel):
    # 20,000 chars is generous for a student's own notes on one case while
    # still bounding request size.
    content: str = Field(default="", max_length=20000)
