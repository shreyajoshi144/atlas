from __future__ import annotations
from typing import Any
import hashlib
from backend.agentic.common import ResearchMode, ToolName
from backend.agentic.planning.models import ResearchPlan, SubQuestion, PlannedAction, EvidenceRequirement, EvidenceType, StoppingCriteria
from backend.evaluation.pipeline_evaluator import run_pipeline_evaluation
from backend.evaluation.testdata.loader import load_scenarios, build_ranked_sources, build_verification_summary


def build_benchmark_plan(scenario: dict[str, Any]) -> ResearchPlan:
    requirements = scenario.get("requirements") or [f"Evidence for {scenario['topic']}"]
    sq = SubQuestion(sub_question_id="sq_1", question=scenario["topic"], rationale="Benchmark coverage")
    actions = [
        PlannedAction(action_id="act_1", tool=ToolName.SEARCH_WEB, purpose="Find evidence for the research question", sub_question_ids=["sq_1"], query=scenario["topic"], max_results=min(10, scenario.get("requested_top_k", 5))),
        PlannedAction(action_id="act_2", tool=ToolName.SCRAPE_SOURCE, purpose="Collect source evidence", sub_question_ids=["sq_1"], depends_on=["act_1"]),
        PlannedAction(action_id="act_3", tool=ToolName.VERIFY_CLAIM, purpose="Verify the main claim against evidence", sub_question_ids=["sq_1"], depends_on=["act_2"]),
    ]
    reqs = [EvidenceRequirement(requirement_id=f"req_{i+1}", sub_question_id="sq_1", description=r, evidence_type=EvidenceType.FACTUAL_CLAIM, min_sources=1) for i, r in enumerate(requirements)]
    return ResearchPlan(plan_id="plan_" + hashlib.sha256(scenario["id"].encode()).hexdigest()[:12], question=scenario["topic"], research_mode=ResearchMode.EXPLORE,
                        objective=f"Answer the research question: {scenario['topic']}", sub_questions=[sq], planned_actions=actions,
                        required_evidence=reqs, stopping_criteria=StoppingCriteria(target_source_count=max(1, scenario.get("requested_top_k", 1)), min_distinct_domains=1,
                        min_requirement_coverage=0.5, min_verified_claim_ratio=0.5, max_tool_calls=10))


def run_benchmark() -> list[dict[str, Any]]:
    rows=[]
    for scenario in load_scenarios():
        summary=run_pipeline_evaluation(topic=scenario["topic"], search_query=scenario["topic"], requested_top_k=scenario.get("requested_top_k",0),
            ranked_sources=build_ranked_sources(scenario), evidence_text=scenario.get("evidence_text",""),
            verification_summary=build_verification_summary(scenario), verification_status="succeeded" if scenario.get("verification_claims") is not None else "unavailable",
            report_markdown=scenario.get("report_markdown",""), key_statistics=scenario.get("key_statistics",[]), research_plan=build_benchmark_plan(scenario),
            replan_count=scenario.get("replan_count",0), agentic_trace=scenario.get("agent_trace",[]), agentic_status="completed", tool_call_count=0)
        rows.append({"id":scenario["id"],"research_quality":summary.research_quality.score if summary.research_quality else None,
                     "agent_quality":summary.agent_quality.score if summary.agent_quality else None,
                     "overall":summary.overall_score,"flags":[f.code for f in summary.flags],"expected_behavior":scenario.get("expected_behavior",{})})
    return rows


def compare_benchmarks(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left={r["id"]:r for r in before}; right={r["id"]:r for r in after}; rows=[]
    for case_id in sorted(set(left)|set(right)):
        a=left.get(case_id,{}); b=right.get(case_id,{})
        rows.append({"id":case_id,"research_quality_delta":(b.get("research_quality")-a.get("research_quality")) if a.get("research_quality") is not None and b.get("research_quality") is not None else None,
                     "agent_quality_delta":(b.get("agent_quality")-a.get("agent_quality")) if a.get("agent_quality") is not None and b.get("agent_quality") is not None else None,
                     "overall_delta":(b.get("overall")-a.get("overall")) if a.get("overall") is not None and b.get("overall") is not None else None})
    return rows
