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
    topic: str = Field(..., min_length=3, max_length=300)
    user_context: str | None = Field(default=None, max_length=3000)
    include_verification: bool = True
    include_executive_brief: bool = True
    persist_memory: bool = True
    top_k_urls: int = Field(default=5, ge=1, le=10)

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("topic must not be empty")
        return cleaned


class ResearchChatRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)
    question: str = Field(..., min_length=3, max_length=2000)
    session_id: str | None = Field(default=None, max_length=120)
    report_id: str | None = Field(default=None, max_length=120)
    history: list[ChatMessageRequest] = Field(default_factory=list)
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
    openai_model: str | None = Field(default=None)
    tavily_max_results: int | None = Field(default=None, ge=1, le=20)
    default_top_k_urls: int | None = Field(default=None, ge=1, le=10)
    scrape_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    scrape_max_retries: int | None = Field(default=None, ge=0, le=5)
