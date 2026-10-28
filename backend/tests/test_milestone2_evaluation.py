import pytest
from types import SimpleNamespace

from backend.agentic.common import ToolName
from backend.agentic.planning.models import ResearchPlan, SubQuestion, PlannedAction, EvidenceRequirement, EvidenceType, StoppingCriteria
from backend.evaluation.benchmark import build_benchmark_plan, compare_benchmarks, run_benchmark
from backend.evaluation.evaluators.agent_quality_evaluator import evaluate_agent_quality
from backend.evaluation.evaluators.citation_evaluator import evaluate_citation_correctness
from backend.evaluation.evaluators.completeness_evaluator import evaluate_completeness
from backend.evaluation.evaluators.performance_evaluator import evaluate_performance


def _plan():
    return ResearchPlan(
        plan_id="plan_123456789abc", question="solar efficiency", research_mode="explore",
        objective="Answer solar efficiency", sub_questions=[SubQuestion(sub_question_id="sq_1", question="solar module efficiency evidence", rationale="benchmark")],
        planned_actions=[
            PlannedAction(action_id="act_1", tool=ToolName.SEARCH_WEB, purpose="search evidence", sub_question_ids=["sq_1"], query="solar efficiency"),
            PlannedAction(action_id="act_2", tool=ToolName.SCRAPE_SOURCE, purpose="scrape evidence", sub_question_ids=["sq_1"], depends_on=["act_1"]),
            PlannedAction(action_id="act_3", tool=ToolName.VERIFY_CLAIM, purpose="verify claim", sub_question_ids=["sq_1"], depends_on=["act_2"]),
        ],
        required_evidence=[EvidenceRequirement(requirement_id="req_1", sub_question_id="sq_1", description="Evidence for solar module efficiency", evidence_type=EvidenceType.QUANTITATIVE_DATA, min_sources=1)],
        stopping_criteria=StoppingCriteria(target_source_count=1, min_distinct_domains=1, min_requirement_coverage=0.5, min_verified_claim_ratio=0.5, max_tool_calls=6),
    )


def test_completeness_is_plan_requirement_based_not_source_count():
    plan = _plan()
    result = evaluate_completeness(research_plan=plan, evidence_text="Solar module efficiency reached 24 percent.", report_markdown="24 percent efficiency was reported.")
    assert result.requirement_coverage == 1.0
    assert result.score == 1.0


def test_completeness_handles_missing_requirement_evidence():
    result = evaluate_completeness(research_plan=_plan(), evidence_text="Unrelated battery text.", report_markdown="No relevant finding.")
    assert result.score == 0.0
    assert any(f.code == "incomplete_research" for f in result.flags)


def test_citation_correctness_detects_mismatch():
    source = SimpleNamespace(url="https://example.com/a")
    result = evaluate_citation_correctness("See https://example.com/b", [source])
    assert result.invalid_citation_count == 1
    assert result.score == 0.0


def test_agent_quality_replanning_recovery_is_deterministic():
    trace = [
        {"action_id":"act_1","tool":"search_web","status":"succeeded"},
        {"action_id":"act_2","tool":"scrape_source","status":"succeeded"},
        {"action_id":"act_3","tool":"verify_claim","status":"succeeded"},
    ]
    agent_trace = [
        {"event":"verification_result","status":"insufficient_evidence","gap_count":1},
        {"event":"replan_created","replan_count":1},
        {"event":"verification_result","status":"supported","gap_count":0},
    ]
    result = evaluate_agent_quality(research_plan=_plan(), tool_trace=trace, replan_count=1, replan_history=[{"replan_count":1}], agentic_trace=agent_trace,
                                    completed_action_ids=["act_1","act_2","act_3"], tool_call_count=3, agentic_status="completed")
    assert result.replan_effectiveness == 1.0
    assert result.successful_recoveries == 1
    assert result.duplicate_research_actions == 0


def test_agent_quality_detects_redundant_research_actions():
    trace = [
        {"action_id":"act_1","tool":"search_web","status":"succeeded"},
        {"action_id":"act_1","tool":"search_web","status":"succeeded"},
    ]
    result = evaluate_agent_quality(research_plan=_plan(), tool_trace=trace, replan_count=0, replan_history=[], agentic_trace=[], completed_action_ids=[], tool_call_count=2, agentic_status="completed")
    assert result.unnecessary_tool_calls == 1
    assert result.tool_efficiency < 1.0


def test_performance_does_not_invent_tokens_or_cost():
    result = evaluate_performance(total_latency_ms=1200, tool_trace=[], tool_call_count=2, replan_count=1,
                                  token_usage={"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}, estimated_cost_usd=0.0)
    assert result.total_latency_ms == 1200
    assert result.total_tokens is None
    assert result.estimated_cost_usd is None


def test_benchmark_has_required_regression_cases_and_is_runnable():
    rows = run_benchmark()
    ids = {r["id"] for r in rows}
    required = {"well_grounded_report","insufficient_evidence","conflicting_sources","verified_without_evidence",
                "citation_mismatch","hallucinated_evidence_and_fabricated_citation","thin_low_coverage_retrieval",
                "multi_step_research","replanning_recovery","unnecessary_replanning","no_sources_returned"}
    assert required <= ids
    assert len(rows) == 11


def test_benchmark_comparison_is_case_aligned():
    before = [{"id":"a","research_quality":0.5,"agent_quality":0.6,"overall":0.53}]
    after = [{"id":"a","research_quality":0.7,"agent_quality":0.8,"overall":0.73}]
    result = compare_benchmarks(before, after)
    assert result == [{"id":"a","research_quality_delta":0.19999999999999996,"agent_quality_delta":0.20000000000000007,"overall_delta":0.19999999999999996}]


def test_evaluation_response_model_exposes_quality_agent_and_performance():
    from backend.evaluation.models import EvaluationSummary, ResearchQualityEvaluation, AgentQualityEvaluation, SystemPerformanceEvaluation
    from backend.models.response_models import ResearchRunResponse, ResearchSessionResponse, ResearchReportResponse
    summary = EvaluationSummary(
        research_quality=ResearchQualityEvaluation(score=0.8),
        agent_quality=AgentQualityEvaluation(plan_completion=1.0, tool_efficiency=0.9, replan_effectiveness=1.0, appropriate_stopping=1.0),
        performance=SystemPerformanceEvaluation(total_latency_ms=1000, tool_call_count=4, replan_count=1),
    )
    response = ResearchRunResponse(
        session=ResearchSessionResponse(session_id="s", topic="t", status="completed", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00"),
        report=ResearchReportResponse(report_id="r", session_id="s", topic_id="local", topic="t", summary="", report_markdown=""),
        top_sources=[], evaluation=summary,
    )
    assert response.evaluation.performance.tool_call_count == 4
    assert response.evaluation.agent_quality.score is None


def test_evaluation_repository_user_scoping(tmp_path, monkeypatch):
    from backend.core import config as config_module
    from backend.database.repositories import ResearchRepository
    from backend.evaluation.models import EvaluationSummary
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "scoped.db"))
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    config_module.get_settings.cache_clear()
    repo = ResearchRepository(); repo.initialize()
    with repo.db.get_connection() as conn:
        for uid, email in [("u1", "u1@example.com"), ("u2", "u2@example.com")]:
            conn.execute("INSERT INTO users (user_id,email,name,password_hash,password_salt,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (uid,email,uid,"h","s","2026-01-01","2026-01-01"))
            conn.execute("INSERT INTO topics (topic_id,user_id,topic,created_at,updated_at) VALUES (?,?,?,?,?)", (uid+"t",uid,"topic","2026-01-01","2026-01-01"))
            conn.execute("INSERT INTO research_sessions (session_id, user_id, topic_id, topic, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)", (uid+"s",uid,uid+"t","topic","completed","2026-01-01","2026-01-01"))
            conn.execute("INSERT INTO reports (report_id,session_id,topic_id,topic,summary,report_markdown,key_statistics_json,swot_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (uid+"r",uid+"s",uid+"t","topic","","","[]","{}","2026-01-01","2026-01-01"))
        conn.commit()
    summary = EvaluationSummary(overall_score=0.5)
    repo.evaluations.save_evaluation("u1r", "u1s", "u1t", summary)
    assert repo.evaluations.get_latest_by_report_id("u1r", user_id="u1") is not None
    assert repo.evaluations.get_latest_by_report_id("u1r", user_id="u2") is None
    config_module.get_settings.cache_clear()


def test_agent_quality_replan_actions_are_required_and_fanout_is_not_penalized():
    plan = _plan()
    trace = [
        {"action_id": "act_1", "tool": "search_web", "status": "succeeded"},
        {"action_id": "act_2", "tool": "scrape_source", "status": "succeeded"},
        {"action_id": "act_2", "tool": "scrape_source", "status": "succeeded"},
        {"action_id": "act_3", "tool": "verify_claim", "status": "succeeded"},
        {"action_id": "replan_1_search", "tool": "request_more_evidence", "status": "succeeded"},
        {"action_id": "replan_1_scrape", "tool": "scrape_source", "status": "succeeded"},
    ]
    history = [{"replan_count": 1, "actions": ["replan_1_search", "replan_1_scrape", "replan_1_verify"], "gap": {}}]
    result = evaluate_agent_quality(
        research_plan=plan, tool_trace=trace, replan_count=1, replan_history=history,
        agentic_trace=[], completed_action_ids=[r["action_id"] for r in trace],
        tool_call_count=len(trace), agentic_status="completed",
    )
    assert result.initial_plan_completion == 1.0
    assert result.adaptive_recovery_completion == pytest.approx(2 / 3, abs=1e-4)
    assert result.plan_completion == pytest.approx(5 / 6, abs=1e-4)
    assert result.unnecessary_tool_calls == 0


def test_agent_quality_does_not_report_full_completion_when_replanned_work_missing():
    plan = _plan()
    history = [{"replan_count": 1, "actions": ["replan_1_search", "replan_1_scrape", "replan_1_verify"]}]
    trace = [
        {"action_id": "act_1", "tool": "search_web", "status": "succeeded"},
        {"action_id": "act_2", "tool": "scrape_source", "status": "succeeded"},
        {"action_id": "act_3", "tool": "verify_claim", "status": "succeeded"},
        {"action_id": "replan_1_search", "tool": "request_more_evidence", "status": "succeeded"},
    ]
    result = evaluate_agent_quality(
        research_plan=plan, tool_trace=trace, replan_count=1, replan_history=history,
        agentic_trace=[], completed_action_ids=["act_1", "act_2", "act_3", "replan_1_search"],
        tool_call_count=4, agentic_status="completed_with_gaps",
    )
    assert result.initial_plan_completion == 1.0
    assert result.adaptive_recovery_completion == pytest.approx(1 / 3, abs=1e-4)
    assert result.plan_completion == pytest.approx(4 / 6, abs=1e-4)
    assert result.plan_completion < 1.0
