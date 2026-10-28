"""Phase 1A — deterministic Research Planner.

Turns ``(question, research_mode)`` into a validated :class:`ResearchPlan`.

Design choices (intentional):
  * Template-driven, **no LLM and no network**: the same input always yields
    the same plan (including ``plan_id``), which makes it cheap, testable and
    safe to run inside the research pipeline on every request.
  * The planner **never executes tools** — it only names them.
  * Output is built from the strict models in ``planning.models``; if a
    template ever produced an inconsistent plan, construction would raise
    ``pydantic.ValidationError`` rather than emit a bad plan.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Optional

from pydantic import Field, field_validator

from backend.agentic.common import ResearchMode, ToolName, compose_query
from backend.agentic.planning.models import (
    EvidenceRequirement,
    EvidenceType,
    PlannedAction,
    ResearchPlan,
    StoppingCriteria,
    SubQuestion,
)
from backend.models.common import BaseSchema

# Mirrors ``MODE_DEFAULT_TOP_K`` in ``backend/api/routes/research.py``. It is
# duplicated (not imported) so the planner stays free of FastAPI/route imports;
# a unit test fails if the two ever drift apart.
MODE_TARGET_SOURCES: dict[str, int] = {
    "brief": 8,
    "explore": 12,
    "compare": 14,
    "investigate": 20,
    "literature_review": 20,
}


class PlanRequest(BaseSchema):
    """Validated planner input (same bounds as ``ResearchRunRequest``)."""

    question: str = Field(min_length=3, max_length=300)
    research_mode: ResearchMode = ResearchMode.EXPLORE
    top_k_urls: Optional[int] = Field(default=None, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def _clean_question(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 3:
            raise ValueError("question must not be empty")
        return cleaned


@dataclass(frozen=True)
class _SubQuestionTemplate:
    question: str  # may use {topic}
    rationale: str
    focus: str  # appended to the topic to form the web-search query
    evidence_type: EvidenceType
    requirement: str


@dataclass(frozen=True)
class _ModeProfile:
    objective: str  # may use {topic}
    templates: tuple[_SubQuestionTemplate, ...]
    min_sources_per_requirement: int
    min_distinct_domains: int
    min_requirement_coverage: float
    min_verified_claim_ratio: float


_T = _SubQuestionTemplate

_PROFILES: dict[str, _ModeProfile] = {
    "brief": _ModeProfile(
        objective="Produce a concise, decision-oriented, source-grounded brief on: {topic}.",
        templates=(
            _T("What are the key facts and current state of {topic}?",
               "Establish the factual baseline the brief rests on.",
               "key facts overview", EvidenceType.FACTUAL_CLAIM,
               "Authoritative sources stating the core facts."),
            _T("What are the most decision-relevant implications of {topic}?",
               "A brief must say what matters for a decision.",
               "implications impact", EvidenceType.EXPERT_ANALYSIS,
               "Analysis that explains practical implications."),
            _T("What are the main risks, limitations or caveats of {topic}?",
               "Keep evidence limitations visible in a short brief.",
               "risks limitations", EvidenceType.COUNTER_EVIDENCE,
               "Sources describing risks, limits or dissent."),
        ),
        min_sources_per_requirement=1,
        min_distinct_domains=3,
        min_requirement_coverage=0.6,
        min_verified_claim_ratio=0.5,
    ),
    "explore": _ModeProfile(
        objective="Build a broad, evidence-backed map of the major themes and perspectives on: {topic}.",
        templates=(
            _T("What are the key facts, definitions and current state of {topic}?",
               "Establish shared ground before exploring perspectives.",
               "overview definition", EvidenceType.FACTUAL_CLAIM,
               "Sources defining the topic and its current state."),
            _T("What are the major themes, perspectives and stakeholders around {topic}?",
               "Exploration should surface the breadth of viewpoints.",
               "perspectives stakeholders", EvidenceType.EXPERT_ANALYSIS,
               "Sources representing distinct viewpoints."),
            _T("What are the most recent developments related to {topic}?",
               "Recency shapes what is currently true.",
               "latest developments", EvidenceType.RECENT_DEVELOPMENT,
               "Recent, dated sources on new developments."),
            _T("What open questions or uncertainties remain about {topic}?",
               "Unresolved areas should be stated, not hidden.",
               "open questions uncertainty", EvidenceType.COUNTER_EVIDENCE,
               "Sources noting disagreement or gaps."),
        ),
        min_sources_per_requirement=2,
        min_distinct_domains=5,
        min_requirement_coverage=0.7,
        min_verified_claim_ratio=0.5,
    ),
    "compare": _ModeProfile(
        objective="Compare the alternatives in '{topic}' across their key dimensions using verified evidence.",
        templates=(),  # built dynamically from detected entities, see _compare_templates
        min_sources_per_requirement=2,
        min_distinct_domains=5,
        min_requirement_coverage=0.75,
        min_verified_claim_ratio=0.6,
    ),
    "investigate": _ModeProfile(
        objective=(
            "Investigate '{topic}' in depth: identify mechanisms and causal evidence, "
            "test claims against counter-evidence, and surface unresolved uncertainties."
        ),
        templates=(
            _T("What is claimed or observed about {topic}, and what is the primary evidence?",
               "Anchor the investigation in primary evidence.",
               "evidence primary data", EvidenceType.PRIMARY_SOURCE,
               "Primary or original sources for the core claims."),
            _T("What mechanisms or causes explain {topic}?",
               "Investigation prioritises causal explanation.",
               "mechanism cause", EvidenceType.EXPERT_ANALYSIS,
               "Expert analysis of mechanisms or causes."),
            _T("What quantitative data or measurements exist for {topic}?",
               "Numbers make claims checkable.",
               "statistics data measurements", EvidenceType.QUANTITATIVE_DATA,
               "Sources with figures that can be checked."),
            _T("What evidence contradicts or challenges the prevailing view of {topic}?",
               "Claims must be tested against counter-evidence.",
               "criticism contradicting evidence", EvidenceType.COUNTER_EVIDENCE,
               "Credible sources that challenge the main claims."),
            _T("What uncertainties and unresolved questions remain about {topic}?",
               "State what the evidence cannot yet settle.",
               "uncertainties unresolved", EvidenceType.COUNTER_EVIDENCE,
               "Sources describing limits of current knowledge."),
        ),
        min_sources_per_requirement=3,
        min_distinct_domains=8,
        min_requirement_coverage=0.8,
        min_verified_claim_ratio=0.7,
    ),
    "literature_review": _ModeProfile(
        objective=(
            "Synthesize the body of literature on '{topic}', separating areas of "
            "agreement from disagreement and noting evidence gaps."
        ),
        templates=(
            _T("What are the core findings reported across the literature on {topic}?",
               "A review starts from what the literature actually reports.",
               "literature review findings", EvidenceType.PRIMARY_SOURCE,
               "Peer-reviewed or primary publications."),
            _T("Where does the literature agree and disagree on {topic}?",
               "Agreement and disagreement are the core synthesis.",
               "agreement disagreement debate", EvidenceType.COUNTER_EVIDENCE,
               "Sources that explicitly contrast findings."),
            _T("What methods and evidence quality underpin the research on {topic}?",
               "Weigh findings by the strength of their methods.",
               "methodology study design", EvidenceType.EXPERT_ANALYSIS,
               "Sources discussing methods or quality."),
            _T("What foundational or seminal works shape {topic}?",
               "Foundational sources anchor the review.",
               "seminal foundational", EvidenceType.PRIMARY_SOURCE,
               "Foundational, widely cited sources."),
            _T("What research gaps remain for {topic}?",
               "Evidence gaps must be reported.",
               "research gaps future work", EvidenceType.COUNTER_EVIDENCE,
               "Sources identifying gaps or open problems."),
        ),
        min_sources_per_requirement=3,
        min_distinct_domains=8,
        min_requirement_coverage=0.8,
        min_verified_claim_ratio=0.6,
    ),
}

_COMPARE_SPLIT = re.compile(r"\s+(?:vs\.?|versus|compared\s+(?:to|with)|against)\s+", re.IGNORECASE)
_COMPARE_PREFIX = re.compile(r"^\s*compare\s+", re.IGNORECASE)
_MAX_COMPARE_ENTITIES = 4


def _detect_entities(topic: str) -> list[str]:
    parts = [p.strip(" ,;:") for p in _COMPARE_SPLIT.split(_COMPARE_PREFIX.sub("", topic))]
    parts = [p for p in parts if len(p) >= 2]
    if 2 <= len(parts) <= _MAX_COMPARE_ENTITIES:
        return parts
    return []


def _compare_templates(topic: str) -> tuple[_SubQuestionTemplate, ...]:
    entities = _detect_entities(topic)
    if not entities:
        return (
            _T("What are the main alternatives or options within {topic}?",
               "A comparison must first identify what is being compared.",
               "alternatives options", EvidenceType.FACTUAL_CLAIM,
               "Sources identifying and defining each alternative."),
            _T("Which dimensions matter most when comparing the options in {topic}?",
               "Comparison needs shared criteria.",
               "comparison criteria dimensions", EvidenceType.EXPERT_ANALYSIS,
               "Analysis naming the decisive comparison criteria."),
            _T("How do the options in {topic} differ on those dimensions?",
               "Head-to-head evidence is the core of the comparison.",
               "comparison differences", EvidenceType.QUANTITATIVE_DATA,
               "Sources comparing the options directly."),
            _T("What trade-offs and conditions favour one option over another in {topic}?",
               "Trade-offs turn a comparison into a decision aid.",
               "trade-offs when to choose", EvidenceType.COUNTER_EVIDENCE,
               "Sources describing trade-offs and caveats."),
        )
    templates: list[_SubQuestionTemplate] = [
        _T(f"What are the documented strengths, weaknesses and key facts for {entity}?",
           f"Each compared option ({entity}) needs its own evidence base.",
           f"{entity} strengths weaknesses", EvidenceType.FACTUAL_CLAIM,
           f"Sources documenting {entity} on its own terms.")
        for entity in entities
    ]
    joined = " and ".join(entities)
    templates.append(
        _T(f"How do {joined} differ on the dimensions that matter most?",
           "Head-to-head evidence is the core of the comparison.",
           "comparison differences", EvidenceType.QUANTITATIVE_DATA,
           "Sources comparing the options directly.")
    )
    templates.append(
        _T(f"What trade-offs and conditions favour one of {joined} over the other?",
           "Trade-offs turn a comparison into a decision aid.",
           "trade-offs when to choose", EvidenceType.COUNTER_EVIDENCE,
           "Sources describing trade-offs and caveats.")
    )
    return tuple(templates)


def _clean_topic(question: str) -> str:
    return " ".join(question.split()).rstrip("?!. ").strip() or question.strip()


def _fill(template: str, topic: str) -> str:
    # str.replace (not .format) so braces in a user's topic can never break it.
    return template.replace("{topic}", topic)


class ResearchPlanner:
    """Builds validated, deterministic research plans. Stateless."""

    def create_plan(
        self,
        question: str,
        research_mode: str | ResearchMode = ResearchMode.EXPLORE,
        top_k_urls: Optional[int] = None,
    ) -> ResearchPlan:
        request = PlanRequest(question=question, research_mode=research_mode, top_k_urls=top_k_urls)
        mode = ResearchMode(request.research_mode).value
        topic = _clean_topic(request.question)
        profile = _PROFILES[mode]
        target = request.top_k_urls if request.top_k_urls is not None else MODE_TARGET_SOURCES[mode]

        templates = _compare_templates(topic) if mode == "compare" else profile.templates

        sub_questions = [
            SubQuestion(
                sub_question_id=f"sq_{i}",
                question=_fill(t.question, topic),
                rationale=t.rationale,
            )
            for i, t in enumerate(templates, start=1)
        ]
        sq_ids = [sq.sub_question_id for sq in sub_questions]

        required_evidence = [
            EvidenceRequirement(
                requirement_id=f"req_{i}",
                sub_question_id=sq_ids[i - 1],
                description=t.requirement,
                evidence_type=t.evidence_type,
                min_sources=profile.min_sources_per_requirement,
            )
            for i, t in enumerate(templates, start=1)
        ]

        planned_actions = self._build_actions(topic, templates, sq_ids, target)

        stopping = StoppingCriteria(
            target_source_count=target,
            min_distinct_domains=min(profile.min_distinct_domains, target),
            min_requirement_coverage=profile.min_requirement_coverage,
            min_verified_claim_ratio=profile.min_verified_claim_ratio,
            # Planned actions plus one spare call per sub-question, reserved for
            # the contingent evidence request. Nothing consumes this yet.
            max_tool_calls=len(planned_actions) + len(sq_ids),
        )

        digest = hashlib.sha256(f"{mode}|{topic.lower()}|{target}".encode("utf-8")).hexdigest()[:12]
        return ResearchPlan(
            plan_id=f"plan_{digest}",
            question=request.question,
            research_mode=mode,
            objective=_fill(profile.objective, topic),
            sub_questions=sub_questions,
            planned_actions=planned_actions,
            required_evidence=required_evidence,
            stopping_criteria=stopping,
        )

    @staticmethod
    def _build_actions(
        topic: str,
        templates: tuple[_SubQuestionTemplate, ...],
        sq_ids: list[str],
        target: int,
    ) -> list[PlannedAction]:
        actions: list[PlannedAction] = []

        def next_id() -> str:
            return f"act_{len(actions) + 1}"

        memory_id = next_id()
        actions.append(
            PlannedAction(
                action_id=memory_id,
                tool=ToolName.SEARCH_EXISTING_EVIDENCE,
                purpose="Reuse relevant evidence already held in vector memory before searching the web.",
                sub_question_ids=list(sq_ids),
                query=compose_query(topic),
                max_results=5,
            )
        )

        per_query = min(20, max(5, math.ceil(target / len(templates)) + 2))
        search_ids: list[str] = []
        for sq_id, template in zip(sq_ids, templates):
            action_id = next_id()
            search_ids.append(action_id)
            actions.append(
                PlannedAction(
                    action_id=action_id,
                    tool=ToolName.SEARCH_WEB,
                    purpose=f"Find sources for {sq_id}: {template.focus}.",
                    sub_question_ids=[sq_id],
                    query=compose_query(topic, template.focus),
                    max_results=per_query,
                    depends_on=[memory_id],
                )
            )

        scrape_id = next_id()
        actions.append(
            PlannedAction(
                action_id=scrape_id,
                tool=ToolName.SCRAPE_SOURCE,
                purpose="Fetch full-page content for the highest-ranked sources found by the searches.",
                sub_question_ids=list(sq_ids),
                max_results=target,
                depends_on=list(search_ids),
            )
        )

        verify_id = next_id()
        actions.append(
            PlannedAction(
                action_id=verify_id,
                tool=ToolName.VERIFY_CLAIM,
                purpose="Verify key claims against the gathered evidence corpus.",
                sub_question_ids=list(sq_ids),
                depends_on=[scrape_id, memory_id],
            )
        )

        actions.append(
            PlannedAction(
                action_id=next_id(),
                tool=ToolName.REQUEST_MORE_EVIDENCE,
                purpose="Close evidence gaps left after verification with a targeted extra search.",
                sub_question_ids=list(sq_ids),
                max_results=5,
                depends_on=[verify_id],
                condition=(
                    "Run only if requirement coverage or the verified-claim ratio "
                    "falls below the stopping criteria after verification."
                ),
            )
        )
        return actions
