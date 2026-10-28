"""Shared enums/helpers for the planner (1A) and the tool registry (1B).

Kept dependency-free (stdlib only) so both packages can import it without
pulling in any service, LLM, or vector-store code.
"""
from __future__ import annotations

from enum import Enum

MAX_QUERY_CHARS = 300


class ResearchMode(str, Enum):
    """Mirrors ``ResearchRunRequest.research_mode`` exactly."""

    BRIEF = "brief"
    EXPLORE = "explore"
    COMPARE = "compare"
    INVESTIGATE = "investigate"
    LITERATURE_REVIEW = "literature_review"


class ToolName(str, Enum):
    """The complete, closed set of tools an agent may ever request."""

    SEARCH_WEB = "search_web"
    SCRAPE_SOURCE = "scrape_source"
    SEARCH_EXISTING_EVIDENCE = "search_existing_evidence"
    VERIFY_CLAIM = "verify_claim"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"


def compose_query(base: str, focus: str = "", limit: int = MAX_QUERY_CHARS) -> str:
    """Join ``base`` and ``focus`` into one search query of at most ``limit``
    characters. ``focus`` is always preserved; ``base`` is trimmed at a word
    boundary when needed. Pure and deterministic.
    """
    base = " ".join(base.split())
    focus = " ".join(focus.split())
    if not focus:
        return base[:limit].rstrip()
    budget = limit - len(focus) - 1
    if budget <= 0:
        return focus[:limit].rstrip()
    if len(base) > budget:
        trimmed = base[:budget]
        if " " in trimmed and base[budget] != " ":
            trimmed = trimmed.rsplit(" ", 1)[0]
        base = trimmed.rstrip()
    return f"{base} {focus}".strip()
