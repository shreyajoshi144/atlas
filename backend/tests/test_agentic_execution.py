"""Focused Phase 1C/1D tests.

These tests use tiny registered handlers only. They do not touch network, LLM,
scraping, vector memory, or the legacy research pipeline.
"""
from __future__ import annotations

import pytest
from backend.agentic.common import ResearchMode, ToolName
from backend.agentic.execution import ActionSelector, AgenticToolExecutor
from backend.agentic.tools.builtin import ToolDependencies, build_default_registry
from backend.agentic.tools.schemas import EvidenceGap, VerificationAssessment
from backend.agentic.planning.models import (
    EvidenceRequirement, EvidenceType, PlannedAction, ResearchPlan,
    StoppingCriteria, SubQuestion,
)
from backend.agentic.tools.base import ToolSpec
from backend.agentic.tools.registry import ToolRegistry
from backend.agentic.tools.schemas import (
    SearchWebInput, SearchWebOutput, ScrapeSourceInput, ScrapeSourceOutput,
    VerifyClaimInput, VerifyClaimOutput, RequestMoreEvidenceInput, RequestMoreEvidenceOutput,
)
from backend.models.domain_models import RankedSource, ScrapedContent, VerificationSummary, VerifiedClaim
from backend.models.domain_models import VerificationStatus


def make_plan(*, max_calls=3, target=1, include_second=True, action_count=None):
    sq = SubQuestion(
        sub_question_id="sq_1",
        question="What are the key facts about Atlas?",
        rationale="Establish the factual baseline.",
    )
    actions = [
        PlannedAction(
            action_id="act_1",
            tool=ToolName.SEARCH_WEB,
            purpose="Search for the factual baseline.",
            sub_question_ids=["sq_1"],
            query="Atlas key facts",
            max_results=1,
        )
    ]
    if include_second:
        actions.append(
            PlannedAction(
                action_id="act_2",
                tool=ToolName.SEARCH_WEB,
                purpose="Run a dependent follow-up search.",
                sub_question_ids=["sq_1"],
                query="Atlas follow up",
                max_results=1,
                depends_on=["act_1"],
            )
        )
    if action_count and action_count >= 3:
        actions.append(
            PlannedAction(
                action_id="act_3",
                tool=ToolName.SEARCH_WEB,
                purpose="Run a final dependent search.",
                sub_question_ids=["sq_1"],
                query="Atlas final",
                max_results=1,
                depends_on=["act_2"],
            )
        )
    return ResearchPlan(
        plan_id="plan_123456789abc",
        question="What are the key facts about Atlas?",
        research_mode=ResearchMode.EXPLORE,
        objective="Produce an evidence-backed answer about Atlas.",
        sub_questions=[sq],
        planned_actions=actions,
        required_evidence=[
            EvidenceRequirement(
                requirement_id="req_1",
                sub_question_id="sq_1",
                description="Sources stating the core facts.",
                evidence_type=EvidenceType.FACTUAL_CLAIM,
                min_sources=1,
            )
        ],
        stopping_criteria=StoppingCriteria(
            target_source_count=target,
            min_distinct_domains=1,
            min_requirement_coverage=0.6,
            min_verified_claim_ratio=0.0,
            max_tool_calls=max_calls,
        ),
    )


def registry(handler=None):
    calls = []

    def search(inp):
        calls.append(inp.query)
        if handler:
            return handler(inp)
        return SearchWebOutput(
            query=inp.query,
            research_mode=inp.research_mode,
            raw_result_count=0,
            sources=[],
        )

    return ToolRegistry([
        ToolSpec(
            name=ToolName.SEARCH_WEB,
            description="test search",
            input_model=SearchWebInput,
            output_model=SearchWebOutput,
            handler=search,
        )
    ]), calls


def test_correct_tool_selection_from_valid_plan():
    plan = make_plan()
    reg, _ = registry()
    decision = ActionSelector(reg).select_next(plan, set(), set(), 0, {})
    assert decision.action is not None
    assert decision.action.action_id == "act_1"
    assert decision.action.tool == ToolName.SEARCH_WEB


def test_dependency_and_order_enforcement():
    plan = make_plan()
    reg, _ = registry()
    selector = ActionSelector(reg)
    blocked = selector.select_next(plan, set(), set(), 0, {})
    assert blocked.action.action_id == "act_1"

    # The second action cannot be selected until act_1 succeeds.
    blocked_second = selector.select_next(plan, {"act_1"}, set(), 1, {})
    assert blocked_second.action is None

    allowed_second = selector.select_next(plan, {"act_1"}, {"act_1"}, 1, {})
    assert allowed_second.action.action_id == "act_2"


def test_only_registered_tools_can_execute():
    plan = make_plan()
    reg, calls = registry()
    # A plan action naming a valid enum but absent from the registry is rejected
    # before a handler can run.
    bad = plan.model_copy(deep=True)
    bad.planned_actions[0].tool = ToolName.SCRAPE_SOURCE
    decision = ActionSelector(reg).select_next(bad, set(), set(), 0, {})
    assert decision.action is None
    assert "not registered" in decision.reason
    assert calls == []


def test_successful_tool_execution_updates_state():
    plan = make_plan(include_second=False)
    reg, calls = registry()
    result = AgenticToolExecutor(reg).run(plan)

    assert result["agentic_status"] == "completed"
    assert result["completed_action_ids"] == ["act_1"]
    assert result["tool_call_count"] == 1
    assert result["tool_results"]["act_1"].success is True
    assert result["tool_trace"][0]["status"] == "succeeded"
    assert result["tool_trace"][0]["tool"] == "search_web"
    assert result["tool_trace"][0]["duration_ms"] >= 0
    assert calls == ["Atlas key facts"]


def test_tool_failure_is_captured_safely():
    def failing(_):
        raise RuntimeError("upstream unavailable")

    plan = make_plan(include_second=False)
    reg, _ = registry(failing)
    result = AgenticToolExecutor(reg).run(plan)

    assert result["agentic_status"] == "completed"
    assert result["tool_results"]["act_1"].success is False
    assert str(result["tool_results"]["act_1"].error.code) == "execution_failed"
    assert result["tool_trace"][0]["status"] == "failed"


def test_tool_action_budget_is_enforced():
    plan = make_plan(max_calls=2, include_second=True)
    reg, _ = registry()
    result = AgenticToolExecutor(reg).run(plan)

    assert result["tool_call_count"] == 2
    assert result["agentic_status"] == "budget_exhausted"
    assert result["completed_action_ids"] == ["act_1", "act_2"]


def test_stopping_criteria_do_not_skip_mandatory_planned_work():
    plan = make_plan(include_second=True)
    reg, calls = registry()
    selector = ActionSelector(reg)
    metrics = {
        "source_count": 1,
        "distinct_domain_count": 1,
        "requirement_coverage": 1.0,
        "verified_claim_ratio": 1.0,
    }
    decision = selector.select_next(plan, {"act_1"}, {"act_1"}, 1, metrics)
    assert decision.action is not None
    assert decision.action.action_id == "act_2"
    assert calls == []


def _source(source_id, url, domain):
    return RankedSource(source_id=source_id, title=source_id, url=url, domain=domain)


def _full_plan(max_calls=6, target=1):
    sq = SubQuestion(
        sub_question_id="sq_1", question="What are the key facts about Atlas?", rationale="Evidence baseline."
    )
    return ResearchPlan(
        plan_id="plan_abcdef123456",
        question="What are the key facts about Atlas?",
        research_mode=ResearchMode.EXPLORE,
        objective="Produce an evidence-backed answer about Atlas.",
        sub_questions=[sq],
        planned_actions=[
            PlannedAction(action_id="act_1", tool=ToolName.SEARCH_WEB, purpose="search", sub_question_ids=["sq_1"], query="atlas", max_results=2),
            PlannedAction(action_id="act_2", tool=ToolName.SCRAPE_SOURCE, purpose="scrape", sub_question_ids=["sq_1"], depends_on=["act_1"]),
            PlannedAction(action_id="act_3", tool=ToolName.VERIFY_CLAIM, purpose="verify", sub_question_ids=["sq_1"], depends_on=["act_2"]),
        ],
        required_evidence=[EvidenceRequirement(requirement_id="req_1", sub_question_id="sq_1", description="Core facts", evidence_type=EvidenceType.FACTUAL_CLAIM, min_sources=1)],
        stopping_criteria=StoppingCriteria(target_source_count=target, min_distinct_domains=1, min_requirement_coverage=0.6, min_verified_claim_ratio=0.0, max_tool_calls=max_calls),
    )

def _full_registry(events):
    sources = [_source("s1", "https://example.com/a", "example.com"), _source("s2", "https://example.org/b", "example.org")]
    def search(inp):
        events.append(("search", inp.query))
        return SearchWebOutput(query=inp.query, research_mode=inp.research_mode, raw_result_count=2, sources=sources)
    def scrape(inp):
        events.append(("scrape", str(inp.url)))
        return ScrapeSourceOutput(source_id=inp.source_id or "unknown", success=True, scraped=ScrapedContent(source_id=inp.source_id or "unknown", url=inp.url, title=inp.title or "", domain=str(inp.url).split('/')[2], content="evidence for claim", content_hash="hash"))
    def verify(inp):
        events.append(("verify", inp.evidence_text))
        claim = VerifiedClaim(claim_text=inp.claim, status=VerificationStatus.VERIFIED, confidence=1.0)
        return VerifyClaimOutput(claim=inp.claim, verification=VerificationSummary(verdict="verified", claims=[claim]))
    return ToolRegistry([
        ToolSpec(name=ToolName.SEARCH_WEB, description="search", input_model=SearchWebInput, output_model=SearchWebOutput, handler=search),
        ToolSpec(name=ToolName.SCRAPE_SOURCE, description="scrape", input_model=ScrapeSourceInput, output_model=ScrapeSourceOutput, handler=scrape),
        ToolSpec(name=ToolName.VERIFY_CLAIM, description="verify", input_model=VerifyClaimInput, output_model=VerifyClaimOutput, handler=verify),
    ])

def test_source_target_does_not_skip_scrape_or_verify():
    events=[]
    result=AgenticToolExecutor(_full_registry(events)).run(_full_plan())
    assert result["agentic_status"] == "completed"
    assert [e[0] for e in events] == ["search", "scrape", "scrape", "verify"]
    assert result["completed_action_ids"] == ["act_1", "act_2", "act_3"]

def test_planned_scrape_processes_all_sources_from_its_search_dependency():
    events=[]
    result=AgenticToolExecutor(_full_registry(events)).run(_full_plan())
    scraped=[e[1] for e in events if e[0] == "scrape"]
    assert scraped == ["https://example.com/a", "https://example.org/b"]
    assert len(result["action_tool_results"]["act_2"]) == 2

def test_verification_executes_after_all_scrape_evidence():
    events=[]
    AgenticToolExecutor(_full_registry(events)).run(_full_plan())
    kinds=[e[0] for e in events]
    assert kinds.index("verify") > kinds.index("scrape")
    assert all(kinds.index("verify") > i for i,k in enumerate(kinds) if k == "scrape")

def test_stopping_only_terminates_after_required_work_is_complete():
    events=[]
    plan=_full_plan()
    result=AgenticToolExecutor(_full_registry(events)).run(plan)
    assert result["agentic_status"] == "completed"
    assert set(result["completed_action_ids"]) == {"act_1", "act_2", "act_3"}
    assert result["execution_metrics"]["source_count"] >= 1


def test_budget_exhaustion_still_works_with_multi_source_scrape():
    events=[]
    result=AgenticToolExecutor(_full_registry(events)).run(_full_plan(max_calls=3))
    assert result["agentic_status"] == "budget_exhausted"
    assert result["tool_call_count"] == 3
    assert result["completed_action_ids"] == ["act_1", "act_2"]
    assert [e[0] for e in events] == ["search", "scrape", "scrape"]


def test_no_arbitrary_code_or_function_execution():
    plan = make_plan(include_second=False)
    reg, calls = registry()
    # The public execution surface takes only a ToolName and validated data;
    # arbitrary Python strings are treated as unknown tools by the registry.
    unknown = reg.execute("os.system", {"query": "echo should-not-run"})
    assert unknown.success is False
    assert str(unknown.error.code) == "unknown_tool"
    assert calls == []


def test_existing_research_pipeline_remains_unchanged():
    # M4: the controlled agentic_execution node sits between plan and the
    # legacy fixed pipeline; every legacy node must still be present, in order.
    try:
        from backend.graph.workflow import build_research_graph
    except ModuleNotFoundError as exc:
        if exc.name == "langgraph":
            return
        raise
    assert list(build_research_graph().get_graph().nodes) == [
        "__start__", "initialize", "plan", "agentic_execution", "search", "scrape",
        "evidence_extraction", "verification", "writer", "credibility",
        "executive_brief_step", "finalize_report", "complete", "__end__",
    ]


def _replan_plan(*, max_calls=10, max_replans=2):
    sq = SubQuestion(sub_question_id="sq_1", question="What are the key facts about Atlas?", rationale="Evidence baseline.")
    return ResearchPlan(
        plan_id="plan_123456789abc",
        question="What are the key facts about Atlas?",
        research_mode=ResearchMode.EXPLORE,
        objective="Produce an evidence-backed answer about Atlas.",
        sub_questions=[sq],
        planned_actions=[
            PlannedAction(action_id="act_1", tool=ToolName.SEARCH_WEB, purpose="search", sub_question_ids=["sq_1"], query="atlas", max_results=1),
            PlannedAction(action_id="act_2", tool=ToolName.SCRAPE_SOURCE, purpose="scrape", sub_question_ids=["sq_1"], depends_on=["act_1"]),
            PlannedAction(action_id="act_3", tool=ToolName.VERIFY_CLAIM, purpose="verify", sub_question_ids=["sq_1"], depends_on=["act_2"]),
        ],
        required_evidence=[EvidenceRequirement(requirement_id="req_1", sub_question_id="sq_1", description="Core facts", evidence_type=EvidenceType.FACTUAL_CLAIM, min_sources=1)],
        stopping_criteria=StoppingCriteria(target_source_count=1, min_distinct_domains=1, min_requirement_coverage=0.6, min_verified_claim_ratio=0.0, max_tool_calls=max_calls),
        max_replans=max_replans,
    )


def _replan_registry(events, verification_statuses):
    sources = [
        _source("s1", "https://example.com/a", "example.com"),
        _source("s2", "https://example.org/b", "example.org"),
        _source("s3", "https://example.net/c", "example.net"),
    ]
    verify_calls = {"n": 0}

    def search(inp):
        events.append(("search", inp.query))
        return SearchWebOutput(query=inp.query, research_mode=inp.research_mode, raw_result_count=1, sources=[sources[0]])

    def request(inp):
        events.append(("request", inp.gap_description))
        return RequestMoreEvidenceOutput(query=inp.question, research_mode=inp.research_mode, filtered_already_seen=0, new_sources=[sources[1]])

    def scrape(inp):
        events.append(("scrape", str(inp.url)))
        return ScrapeSourceOutput(source_id=inp.source_id or "unknown", success=True, scraped=ScrapedContent(source_id=inp.source_id or "unknown", url=inp.url, title=inp.title or "", domain=str(inp.url).split('/')[2], content="new evidence", content_hash=f"hash-{inp.source_id}"))

    def verify(inp):
        idx = verify_calls["n"]
        verify_calls["n"] += 1
        status = verification_statuses[min(idx, len(verification_statuses) - 1)]
        events.append(("verify", status))
        claim_status = {
            "supported": VerificationStatus.VERIFIED,
            "conflicting": VerificationStatus.CONFLICTING,
            "not_verified": VerificationStatus.NOT_VERIFIED,
            "unsupported_claim": VerificationStatus.NOT_VERIFIED,
            "insufficient_evidence": VerificationStatus.INSUFFICIENT_EVIDENCE,
        }[status]
        claim = VerifiedClaim(claim_text=inp.claim, status=claim_status, confidence=1.0 if status == "supported" else 0.2)
        gaps = [] if status == "supported" else [EvidenceGap(
            claim=inp.claim,
            missing_evidence="Independent evidence directly supporting the claim.",
            reason=f"Verification status: {status}.",
            preferred_tool=ToolName.REQUEST_MORE_EVIDENCE.value,
        )]
        assessment_status = {
            "supported": "supported",
            "conflicting": "conflicting_evidence",
            "not_verified": "unsupported_claim",
            "unsupported_claim": "unsupported_claim",
            "insufficient_evidence": "insufficient_evidence",
        }[status]
        return VerifyClaimOutput(
            claim=inp.claim,
            verification=VerificationSummary(verdict=status, claims=[claim]),
            assessment=VerificationAssessment(status=assessment_status, rationale=f"test: {status}"),
            evidence_gaps=gaps,
        )

    return ToolRegistry([
        ToolSpec(name=ToolName.SEARCH_WEB, description="search", input_model=SearchWebInput, output_model=SearchWebOutput, handler=search),
        ToolSpec(name=ToolName.REQUEST_MORE_EVIDENCE, description="targeted search", input_model=RequestMoreEvidenceInput, output_model=RequestMoreEvidenceOutput, handler=request),
        ToolSpec(name=ToolName.SCRAPE_SOURCE, description="scrape", input_model=ScrapeSourceInput, output_model=ScrapeSourceOutput, handler=scrape),
        ToolSpec(name=ToolName.VERIFY_CLAIM, description="verify", input_model=VerifyClaimInput, output_model=VerifyClaimOutput, handler=verify),
    ])


def test_verification_passes_with_zero_unnecessary_replans():
    events = []
    result = AgenticToolExecutor(_replan_registry(events, ["supported"])).run(_replan_plan())
    assert result["replan_count"] == 0
    assert [e[0] for e in events] == ["search", "scrape", "verify"]
    assert result["verification_status"] == "supported"


def test_insufficient_evidence_triggers_targeted_replan_and_reverification():
    events = []
    result = AgenticToolExecutor(_replan_registry(events, ["insufficient_evidence", "supported"])).run(_replan_plan())
    assert result["replan_count"] == 1
    assert [e[0] for e in events] == ["search", "scrape", "verify", "request", "scrape", "verify"]
    assert result["verification_status"] == "supported"
    assert result["agentic_status"] == "completed"
    assert result["newly_discovered_sources"][0].source_id == "s2"
    assert any(e["event"] == "evidence_gap_detected" for e in result["agentic_trace"])
    assert any(e["event"] == "replan_created" for e in result["agentic_trace"])


@pytest.mark.parametrize("status", ["conflicting", "not_verified"])
def test_conflicting_or_unsupported_claim_triggers_replan(status):
    events = []
    result = AgenticToolExecutor(_replan_registry(events, [status, "supported"])).run(_replan_plan())
    assert result["replan_count"] == 1
    assert result["verification_status"] == "supported"
    assert [e[0] for e in events].count("request") == 1


def test_replan_uses_only_registered_tools():
    events = []
    registry_without_request = ToolRegistry([
        ToolSpec(name=ToolName.SEARCH_WEB, description="search", input_model=SearchWebInput, output_model=SearchWebOutput, handler=lambda inp: SearchWebOutput(query=inp.query, research_mode=inp.research_mode, raw_result_count=0, sources=[])),
        ToolSpec(name=ToolName.SCRAPE_SOURCE, description="scrape", input_model=ScrapeSourceInput, output_model=ScrapeSourceOutput, handler=lambda inp: ScrapeSourceOutput(source_id=inp.source_id or "x", success=False, error_message="x")),
        ToolSpec(name=ToolName.VERIFY_CLAIM, description="verify", input_model=VerifyClaimInput, output_model=VerifyClaimOutput, handler=lambda inp: VerifyClaimOutput(claim=inp.claim, verification=VerificationSummary(verdict="insufficient", claims=[]))),
    ])
    result = AgenticToolExecutor(registry_without_request).run(_replan_plan(max_calls=6))
    assert result["replan_count"] == 0
    assert all(t["tool"] != ToolName.REQUEST_MORE_EVIDENCE.value for t in result["tool_trace"])


def test_duplicate_replan_is_prevented_and_repeated_failure_terminates_safely():
    events = []
    result = AgenticToolExecutor(_replan_registry(events, ["insufficient_evidence"])).run(_replan_plan(max_calls=10, max_replans=3))
    assert result["replan_count"] == 1
    assert result["agentic_status"] == "completed_with_gaps"
    assert [e[0] for e in events].count("request") == 1


def test_max_replans_are_hard_bounded():
    events = []
    result = AgenticToolExecutor(_replan_registry(events, ["insufficient_evidence"])).run(_replan_plan(max_calls=20, max_replans=1))
    assert result["replan_count"] == 1
    assert result["agentic_status"] == "completed_with_gaps"
    assert [e[0] for e in events].count("request") == 1


def test_new_evidence_reaches_reverification_and_success_exits_loop():
    events = []
    result = AgenticToolExecutor(_replan_registry(events, ["unsupported_claim", "supported"])).run(_replan_plan())
    verify_indexes = [i for i, event in enumerate(events) if event[0] == "verify"]
    assert len(verify_indexes) == 2
    assert events[verify_indexes[-1]][1] == "supported"
    assert result["current_action_id"] is None
    assert result["agentic_status"] == "completed"


@pytest.mark.parametrize(
    ("claim_status", "expected"),
    [
        (VerificationStatus.VERIFIED, "supported"),
        (VerificationStatus.INSUFFICIENT_EVIDENCE, "insufficient_evidence"),
        (VerificationStatus.CONFLICTING, "conflicting_evidence"),
        (VerificationStatus.NOT_VERIFIED, "unsupported_claim"),
    ],
)
def test_verification_adapter_emits_structured_evidence_gap_status(claim_status, expected):
    class FakeVerificationAgent:
        def verify(self, report_markdown, scraped_research_text):
            claim = VerifiedClaim(claim_text=report_markdown, status=claim_status, confidence=0.5)
            return VerificationSummary(verdict=str(claim_status), claims=[claim])

    registry = build_default_registry(ToolDependencies(verification_agent=lambda: FakeVerificationAgent()))
    result = registry.execute(
        ToolName.VERIFY_CLAIM,
        {"claim": "Atlas requires evidence.", "evidence_text": "Evidence text."},
    )
    assert result.success is True
    output = result.output
    assert isinstance(output, VerifyClaimOutput)
    assert output.assessment is not None
    assert output.assessment.status == expected
    assert (not output.evidence_gaps) if expected == "supported" else bool(output.evidence_gaps)
