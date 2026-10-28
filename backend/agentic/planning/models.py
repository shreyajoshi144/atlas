"""Phase 1A — strict schema for a ResearchPlan.

A plan is *data only*: it names the tools an agent intends to use but never
calls them. Every model inherits ``BaseSchema`` (``extra="forbid"``), so any
unknown field is rejected. Cross-field rules (dangling ids, data-flow between
tools, uncovered sub-questions, ...) are enforced by ``ResearchPlan``'s
model validator, so a plan that parses is internally consistent.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import Field, model_validator

from backend.agentic.common import ResearchMode, ToolName
from backend.models.common import BaseSchema

PLAN_SCHEMA_VERSION = "1.0"

# Tools whose output can feed a downstream step.
_SOURCE_PRODUCERS = {ToolName.SEARCH_WEB, ToolName.REQUEST_MORE_EVIDENCE}
_EVIDENCE_PRODUCERS = {ToolName.SCRAPE_SOURCE, ToolName.SEARCH_EXISTING_EVIDENCE}
_QUERY_TOOLS = {ToolName.SEARCH_WEB, ToolName.SEARCH_EXISTING_EVIDENCE}


class EvidenceType(str, Enum):
    FACTUAL_CLAIM = "factual_claim"
    QUANTITATIVE_DATA = "quantitative_data"
    EXPERT_ANALYSIS = "expert_analysis"
    PRIMARY_SOURCE = "primary_source"
    COUNTER_EVIDENCE = "counter_evidence"
    RECENT_DEVELOPMENT = "recent_development"


class SubQuestion(BaseSchema):
    sub_question_id: str = Field(pattern=r"^sq_\d{1,2}$")
    question: str = Field(min_length=10, max_length=600)
    rationale: str = Field(min_length=5, max_length=300)


class PlannedAction(BaseSchema):
    """One *intended* tool call. ``query``/``max_results`` are planning hints,
    not executable arguments — the real arguments are validated against the
    tool's input schema at execution time (Phase 1B)."""

    action_id: str = Field(pattern=r"^act_\d{1,3}$")
    tool: ToolName
    purpose: str = Field(min_length=5, max_length=300)
    sub_question_ids: list[str] = Field(min_length=1, max_length=8)
    query: Optional[str] = Field(default=None, min_length=3, max_length=300)
    max_results: Optional[int] = Field(default=None, ge=1, le=20)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    condition: Optional[str] = Field(default=None, min_length=5, max_length=300)

    @model_validator(mode="after")
    def _check_tool_specific_fields(self) -> "PlannedAction":
        tool = ToolName(self.tool)
        if tool in _QUERY_TOOLS and self.query is None:
            raise ValueError(f"{tool.value} requires a query")
        if tool not in _QUERY_TOOLS and self.query is not None:
            raise ValueError(f"{tool.value} must not set a query")
        if tool == ToolName.VERIFY_CLAIM and self.max_results is not None:
            raise ValueError("verify_claim must not set max_results")
        if tool == ToolName.REQUEST_MORE_EVIDENCE and self.condition is None:
            raise ValueError("request_more_evidence is contingent and requires a condition")
        if tool != ToolName.REQUEST_MORE_EVIDENCE and self.condition is not None:
            raise ValueError("only request_more_evidence may carry a condition")
        if len(set(self.sub_question_ids)) != len(self.sub_question_ids):
            raise ValueError("sub_question_ids must not contain duplicates")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on must not contain duplicates")
        if self.action_id in self.depends_on:
            raise ValueError("an action cannot depend on itself")
        return self


class EvidenceRequirement(BaseSchema):
    requirement_id: str = Field(pattern=r"^req_\d{1,3}$")
    sub_question_id: str = Field(pattern=r"^sq_\d{1,2}$")
    description: str = Field(min_length=5, max_length=400)
    evidence_type: EvidenceType
    min_sources: int = Field(default=2, ge=1, le=10)


class StoppingCriteria(BaseSchema):
    target_source_count: int = Field(ge=1, le=20)
    min_distinct_domains: int = Field(ge=1, le=20)
    min_requirement_coverage: float = Field(ge=0.0, le=1.0)
    min_verified_claim_ratio: float = Field(ge=0.0, le=1.0)
    max_tool_calls: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _domains_within_target(self) -> "StoppingCriteria":
        if self.min_distinct_domains > self.target_source_count:
            raise ValueError("min_distinct_domains cannot exceed target_source_count")
        return self


class ResearchPlan(BaseSchema):
    schema_version: Literal["1.0"] = PLAN_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^plan_[0-9a-f]{12}$")
    question: str = Field(min_length=3, max_length=300)
    research_mode: ResearchMode
    objective: str = Field(min_length=10, max_length=500)
    sub_questions: list[SubQuestion] = Field(min_length=1, max_length=8)
    planned_actions: list[PlannedAction] = Field(min_length=1, max_length=40)
    required_evidence: list[EvidenceRequirement] = Field(min_length=1, max_length=24)
    stopping_criteria: StoppingCriteria
    max_replans: int = Field(default=2, ge=0, le=3)

    @model_validator(mode="after")
    def _check_consistency(self) -> "ResearchPlan":
        sq_ids = [sq.sub_question_id for sq in self.sub_questions]
        if len(set(sq_ids)) != len(sq_ids):
            raise ValueError("duplicate sub_question_id")
        sq_set = set(sq_ids)

        action_ids = [a.action_id for a in self.planned_actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("duplicate action_id")

        req_ids = [r.requirement_id for r in self.required_evidence]
        if len(set(req_ids)) != len(req_ids):
            raise ValueError("duplicate requirement_id")

        for req in self.required_evidence:
            if req.sub_question_id not in sq_set:
                raise ValueError(
                    f"{req.requirement_id} references unknown sub_question_id {req.sub_question_id}"
                )

        seen: dict[str, PlannedAction] = {}
        covered: set[str] = set()
        for action in self.planned_actions:
            unknown = set(action.sub_question_ids) - sq_set
            if unknown:
                raise ValueError(f"{action.action_id} references unknown sub_question_ids {sorted(unknown)}")
            covered.update(action.sub_question_ids)

            # Dependencies must point at *earlier* actions: this fixes a
            # valid execution order and makes cycles impossible.
            for dep in action.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"{action.action_id} depends on {dep}, which is unknown or not earlier in the plan"
                    )

            # Data-flow rules: a scrape needs a source producer upstream and a
            # verification needs evidence upstream.
            dep_tools = {ToolName(seen[d].tool) for d in action.depends_on}
            tool = ToolName(action.tool)
            if tool == ToolName.SCRAPE_SOURCE and not (dep_tools & _SOURCE_PRODUCERS):
                raise ValueError(
                    f"{action.action_id} (scrape_source) must depend on a search_web or request_more_evidence action"
                )
            if tool == ToolName.VERIFY_CLAIM and not (dep_tools & _EVIDENCE_PRODUCERS):
                raise ValueError(
                    f"{action.action_id} (verify_claim) must depend on a scrape_source or search_existing_evidence action"
                )
            seen[action.action_id] = action

        if not any(ToolName(a.tool) in {ToolName.SEARCH_WEB, ToolName.SEARCH_EXISTING_EVIDENCE} for a in self.planned_actions):
            raise ValueError("plan must contain at least one search_web or search_existing_evidence action")

        missing_actions = sq_set - covered
        if missing_actions:
            raise ValueError(f"sub-questions without any planned action: {sorted(missing_actions)}")
        missing_evidence = sq_set - {r.sub_question_id for r in self.required_evidence}
        if missing_evidence:
            raise ValueError(f"sub-questions without any required evidence: {sorted(missing_evidence)}")

        if self.stopping_criteria.max_tool_calls < len(self.planned_actions):
            raise ValueError("stopping_criteria.max_tool_calls must be >= the number of planned actions")
        return self
