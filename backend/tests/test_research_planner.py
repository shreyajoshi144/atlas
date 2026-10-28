"""Phase 1A — Research Planner: valid output and strict rejection of invalid output."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from backend.agentic.common import ResearchMode, ToolName
from backend.agentic.planning import (
    MODE_TARGET_SOURCES,
    PlanRequest,
    ResearchPlan,
    ResearchPlanner,
)

MODES = ["brief", "explore", "compare", "investigate", "literature_review"]
TOPIC = "State of solid-state battery commercialization"


def _plan(mode: str = "explore", question: str = TOPIC, **kw) -> ResearchPlan:
    return ResearchPlanner().create_plan(question, mode, **kw)


def _valid_dict(mode: str = "brief") -> dict:
    return _plan(mode).model_dump(mode="json")


def _action(d: dict, tool: str) -> dict:
    return next(a for a in d["planned_actions"] if a["tool"] == tool)


# ── 1. valid planner output ──────────────────────────────────────────────────
@pytest.mark.parametrize("mode", MODES)
def test_every_mode_yields_a_complete_valid_plan(mode):
    plan = _plan(mode)
    assert isinstance(plan, ResearchPlan)
    assert plan.research_mode == mode
    assert plan.question == TOPIC
    assert plan.objective and TOPIC in plan.objective
    assert plan.sub_questions and plan.planned_actions and plan.required_evidence
    assert plan.stopping_criteria.target_source_count == MODE_TARGET_SOURCES[mode]
    # Re-validating the serialised plan must succeed and round-trip exactly.
    assert ResearchPlan.model_validate_json(plan.model_dump_json()) == plan


def test_planner_targets_match_the_api_mode_defaults():
    """Guard against drift: the planner mirrors MODE_DEFAULT_TOP_K by value."""
    from backend.api.routes.research import MODE_DEFAULT_TOP_K

    assert MODE_TARGET_SOURCES == MODE_DEFAULT_TOP_K


@pytest.mark.parametrize("mode", MODES)
def test_planner_is_deterministic(mode):
    a, b = _plan(mode), _plan(mode)
    assert a.model_dump() == b.model_dump()
    assert a.plan_id == b.plan_id


def test_plan_id_changes_with_mode_topic_and_target():
    ids = {
        _plan("explore").plan_id,
        _plan("investigate").plan_id,
        _plan("explore", "A different question entirely").plan_id,
        _plan("explore", top_k_urls=5).plan_id,
    }
    assert len(ids) == 4


@pytest.mark.parametrize("mode", MODES)
def test_action_graph_is_ordered_and_uses_only_registered_tools(mode):
    plan = _plan(mode)
    actions = plan.planned_actions
    assert {a.tool for a in actions} <= {t.value for t in ToolName}
    assert actions[0].tool == ToolName.SEARCH_EXISTING_EVIDENCE.value
    assert actions[-1].tool == ToolName.REQUEST_MORE_EVIDENCE.value
    assert actions[-1].condition  # contingent, never unconditional
    searches = [a for a in actions if a.tool == ToolName.SEARCH_WEB.value]
    assert len(searches) == len(plan.sub_questions)
    scrape = _action(plan.model_dump(mode="json"), "scrape_source")
    assert set(scrape["depends_on"]) == {a.action_id for a in searches}
    earlier: set[str] = set()
    for a in actions:
        assert set(a.depends_on) <= earlier
        earlier.add(a.action_id)


@pytest.mark.parametrize("mode", MODES)
def test_every_sub_question_has_actions_and_required_evidence(mode):
    plan = _plan(mode)
    for sq in plan.sub_questions:
        assert any(sq.sub_question_id in a.sub_question_ids for a in plan.planned_actions)
        assert any(r.sub_question_id == sq.sub_question_id for r in plan.required_evidence)


def test_top_k_override_is_respected_and_domains_never_exceed_it():
    plan = _plan("investigate", top_k_urls=3)
    assert plan.stopping_criteria.target_source_count == 3
    assert plan.stopping_criteria.min_distinct_domains <= 3


def test_question_is_normalised_and_braces_are_safe():
    plan = _plan("explore", "  What is   {weird} in AI?  ")
    assert plan.question == "What is {weird} in AI?"
    assert "{weird}" in plan.objective
    searches = [a for a in plan.planned_actions if a.query]
    assert all("?" not in a.query and "  " not in a.query for a in searches)


def test_compare_mode_detects_entities():
    plan = _plan("compare", "Postgres vs MySQL vs SQLite")
    # one sub-question per entity + head-to-head + trade-offs
    assert len(plan.sub_questions) == 5
    joined = " ".join(sq.question for sq in plan.sub_questions)
    for entity in ("Postgres", "MySQL", "SQLite"):
        assert entity in joined


@pytest.mark.parametrize("question", ["Best database for analytics", "A vs B vs C vs D vs E"])
def test_compare_mode_falls_back_to_generic_sub_questions(question):
    plan = _plan("compare", question)
    assert len(plan.sub_questions) == 4
    assert plan.research_mode == "compare"


@pytest.mark.parametrize("mode", MODES)
def test_maximum_length_question_still_yields_a_valid_plan(mode):
    plan = _plan(mode, "x" * 4 + " " + "topic " * 48)  # ~290 chars
    assert all(len(a.query) <= 300 for a in plan.planned_actions if a.query)


def test_planning_never_executes_a_tool(monkeypatch):
    from backend.agentic.tools.registry import ToolRegistry

    def boom(*_a, **_k):
        raise AssertionError("planner must not execute tools")

    monkeypatch.setattr(ToolRegistry, "execute", boom)
    for mode in MODES:
        _plan(mode)


# ── 2. invalid planner output is rejected ────────────────────────────────────
def _drop_objective(d): d.pop("objective")
def _extra_field(d): d["unexpected"] = 1
def _no_sub_questions(d): d["sub_questions"] = []
def _unknown_tool(d): d["planned_actions"][1]["tool"] = "run_python"
def _dangling_action_sq(d): d["planned_actions"][1]["sub_question_ids"] = ["sq_9"]
def _dangling_requirement_sq(d): d["required_evidence"][0]["sub_question_id"] = "sq_9"
def _forward_dependency(d): d["planned_actions"][0]["depends_on"] = ["act_3"]
def _unknown_dependency(d): d["planned_actions"][1]["depends_on"] = ["act_99"]
def _self_dependency(d): d["planned_actions"][1]["depends_on"] = ["act_2"]
def _duplicate_action_id(d): d["planned_actions"][2]["action_id"] = "act_2"
def _duplicate_sq_id(d): d["sub_questions"][1]["sub_question_id"] = "sq_1"
def _duplicate_requirement_id(d): d["required_evidence"][1]["requirement_id"] = "req_1"
def _bad_sq_id_format(d): d["sub_questions"][0]["sub_question_id"] = "question_1"
def _uncovered_by_action(d):
    d["sub_questions"].append({"sub_question_id": "sq_9", "question": "An extra uncovered question?", "rationale": "extra rationale"})
    d["required_evidence"].append({"requirement_id": "req_9", "sub_question_id": "sq_9", "description": "Some evidence needed", "evidence_type": "factual_claim", "min_sources": 1})
def _uncovered_by_evidence(d):
    d["sub_questions"].append({"sub_question_id": "sq_9", "question": "An extra uncovered question?", "rationale": "extra rationale"})
    d["planned_actions"][1]["sub_question_ids"].append("sq_9")
def _scrape_without_search(d): _action(d, "scrape_source")["depends_on"] = ["act_1"]
def _verify_without_evidence(d): _action(d, "verify_claim")["depends_on"] = []
def _rme_without_condition(d): _action(d, "request_more_evidence")["condition"] = None
def _condition_on_search(d): _action(d, "search_web")["condition"] = "run it sometimes maybe"
def _search_without_query(d): _action(d, "search_web")["query"] = None
def _verify_with_query(d): _action(d, "verify_claim")["query"] = "not allowed here"
def _verify_with_max_results(d): _action(d, "verify_claim")["max_results"] = 3
def _scrape_with_query(d): _action(d, "scrape_source")["query"] = "not allowed here"
def _bad_mode(d): d["research_mode"] = "deep_dive"
def _target_zero(d): d["stopping_criteria"]["target_source_count"] = 0
def _target_over_cap(d): d["stopping_criteria"]["target_source_count"] = 21
def _coverage_over_one(d): d["stopping_criteria"]["min_requirement_coverage"] = 1.5
def _ratio_negative(d): d["stopping_criteria"]["min_verified_claim_ratio"] = -0.1
def _tool_calls_too_low(d): d["stopping_criteria"]["max_tool_calls"] = 1
def _domains_exceed_target(d): d["stopping_criteria"]["min_distinct_domains"] = d["stopping_criteria"]["target_source_count"] + 1
def _bad_plan_id(d): d["plan_id"] = "plan_xyz"
def _bad_schema_version(d): d["schema_version"] = "2.0"
def _wrong_type(d): d["sub_questions"] = "not a list"
def _bad_evidence_type(d): d["required_evidence"][0]["evidence_type"] = "vibes"
def _min_sources_zero(d): d["required_evidence"][0]["min_sources"] = 0
def _empty_actions(d): d["planned_actions"] = []
def _no_search_action(d):
    d["planned_actions"] = [a for a in d["planned_actions"] if a["tool"] in {"request_more_evidence"}]
    d["planned_actions"][0]["depends_on"] = []

INVALID_MUTATIONS = [
    _drop_objective, _extra_field, _no_sub_questions, _unknown_tool, _dangling_action_sq,
    _dangling_requirement_sq, _forward_dependency, _unknown_dependency, _self_dependency,
    _duplicate_action_id, _duplicate_sq_id, _duplicate_requirement_id, _bad_sq_id_format,
    _uncovered_by_action, _uncovered_by_evidence, _scrape_without_search, _verify_without_evidence,
    _rme_without_condition, _condition_on_search, _search_without_query, _verify_with_query,
    _verify_with_max_results, _scrape_with_query, _bad_mode, _target_zero, _target_over_cap,
    _coverage_over_one, _ratio_negative, _tool_calls_too_low, _domains_exceed_target,
    _bad_plan_id, _bad_schema_version, _wrong_type, _bad_evidence_type, _min_sources_zero,
    _empty_actions, _no_search_action,
]


# Each mutation must be rejected for its *intended* reason, not incidentally.
EXPECTED_ERROR = {
    _drop_objective: "Field required", _extra_field: "Extra inputs are not permitted",
    _no_sub_questions: "at least 1 item", _unknown_tool: "Input should be",
    _dangling_action_sq: "unknown sub_question_ids", _dangling_requirement_sq: "unknown sub_question_id",
    _forward_dependency: "unknown or not earlier", _unknown_dependency: "unknown or not earlier",
    _self_dependency: "cannot depend on itself", _duplicate_action_id: "duplicate action_id",
    _duplicate_sq_id: "duplicate sub_question_id", _duplicate_requirement_id: "duplicate requirement_id",
    _bad_sq_id_format: "should match pattern", _uncovered_by_action: "without any planned action",
    _uncovered_by_evidence: "without any required evidence", _scrape_without_search: "must depend on a search_web",
    _verify_without_evidence: "must depend on a scrape_source", _rme_without_condition: "requires a condition",
    _condition_on_search: "only request_more_evidence may carry", _search_without_query: "requires a query",
    _verify_with_query: "must not set a query", _verify_with_max_results: "must not set max_results",
    _scrape_with_query: "must not set a query", _bad_mode: "Input should be",
    _target_zero: "greater than or equal to 1", _target_over_cap: "less than or equal to 20",
    _coverage_over_one: "less than or equal to 1", _ratio_negative: "greater than or equal to 0",
    _tool_calls_too_low: "max_tool_calls", _domains_exceed_target: "cannot exceed target_source_count",
    _bad_plan_id: "should match pattern", _bad_schema_version: "Input should be '1.0'",
    _wrong_type: "valid list", _bad_evidence_type: "Input should be", _min_sources_zero: "greater than or equal to 1",
    _empty_actions: "at least 1 item", _no_search_action: "at least one search_web",
}


def test_every_mutation_has_an_expected_error():
    assert set(EXPECTED_ERROR) == set(INVALID_MUTATIONS)


@pytest.mark.parametrize("mutation", INVALID_MUTATIONS, ids=lambda f: f.__name__.lstrip("_"))
def test_invalid_plan_payloads_are_rejected(mutation):
    payload = copy.deepcopy(_valid_dict("brief"))
    ResearchPlan.model_validate(payload)  # the unmutated payload is valid
    mutation(payload)
    with pytest.raises(ValidationError, match=EXPECTED_ERROR[mutation]):
        ResearchPlan.model_validate(payload)


@pytest.mark.parametrize("raw", ["not json at all", '{"question": "half a plan"', "[]", "null", "{}"])
def test_malformed_json_plans_are_rejected(raw):
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate_json(raw)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"question": ""},
        {"question": "  "},
        {"question": "ab"},
        {"question": "x" * 301},
        {"question": TOPIC, "research_mode": "deep_dive"},
        {"question": TOPIC, "top_k_urls": 0},
        {"question": TOPIC, "top_k_urls": 21},
    ],
)
def test_invalid_planner_inputs_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        ResearchPlanner().create_plan(**kwargs)
    with pytest.raises(ValidationError):
        PlanRequest(**kwargs)


def test_planner_input_accepts_enum_and_string_modes():
    assert _plan(ResearchMode.COMPARE, "A vs B").research_mode == "compare"
    assert _plan("compare", "A vs B").research_mode == "compare"
