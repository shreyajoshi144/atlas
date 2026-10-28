"""M9 — policy validation, fallback eligibility, AtlasBench evidence and the pure autopilot selector."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from backend.bench.models import EVALUATOR_VERSION, allowed_models
from backend.routing import selector
from backend.routing.evidence import ModelEvidence, evidence_from_run, load_evidence
from backend.routing.failures import classify_failure
from backend.routing.models import RoutingPolicy, check_models, default_policy

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
BIG, SMALL, MID, HUGE = "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b"
ALLOWED = allowed_models()


def ev(model, quality=0.9, latency=900.0, completion=200.0, prompt=1500.0, cases=8, run="a", **kw) -> ModelEvidence:
    return ModelEvidence(model=model, run_id=f"run_{run * 32}", completed_at="2026-10-05T00:00:00+00:00", strategy="single_pass", quality=quality, cases=cases,
                         completion_rate=1.0, mean_call_latency_ms=latency, avg_prompt_tokens=prompt, avg_completion_tokens=completion, **kw)


def auto(**kw) -> RoutingPolicy:
    return RoutingPolicy(**{"name": "auto", "mode": "autopilot", "model": BIG, "min_quality": 0.8, "max_latency_ms": 4000, "max_cost_per_call_usd": 0.05, **kw})


# ── policy definition ────────────────────────────────────────────────────────
def test_default_policy_is_the_application_model_with_no_fallback():
    p = default_policy(BIG)
    assert p.mode == "explicit" and p.model == BIG and p.fallback_enabled is False


@pytest.mark.parametrize("bad,needle", [
    ({"fallback_enabled": True}, "fallback_models is empty"),
    ({"fallback_models": [BIG]}, "primary model"),                       # explicit mode: the primary cannot be its own fallback
    ({"fallback_models": [SMALL, SMALL]}, "duplicates"),
    ({"candidate_models": [SMALL, SMALL]}, "duplicates"),
    ({"name": "   "}, "at least 1 character"),
    ({"min_quality": 1.5}, "less than or equal"),
    ({"max_latency_ms": 10}, "greater than or equal"),
    ({"max_cost_per_call_usd": 0}, "greater than or equal"),
    ({"fallback_models": [SMALL, MID, HUGE, "x"]}, "at most 3"),
    ({"mode": "router"}, "explicit"),
    ({"surprise": 1}, "Extra inputs"),
])
def test_invalid_policies_are_rejected(bad, needle):
    with pytest.raises(ValidationError) as e:
        RoutingPolicy(**{"name": "p", "model": BIG, **bad})
    assert needle.lower() in str(e.value).lower()


def test_in_autopilot_the_default_model_may_be_a_fallback():
    p = RoutingPolicy(name="a", mode="autopilot", model=BIG, fallback_enabled=True, fallback_models=[BIG, SMALL])
    assert p.fallback_models == [BIG, SMALL]


def test_policies_may_only_reference_allowed_models():
    check_models(RoutingPolicy(name="ok", model=BIG, fallback_enabled=True, fallback_models=[SMALL]), ALLOWED)
    with pytest.raises(ValueError, match="gpt-9"):
        check_models(RoutingPolicy(name="x", model="gpt-9"), ALLOWED)
    with pytest.raises(ValueError, match="not allowed"):
        check_models(RoutingPolicy(name="x", model=BIG, fallback_enabled=True, fallback_models=["evil-model"]), ALLOWED)
    with pytest.raises(ValueError, match="not allowed"):
        check_models(RoutingPolicy(name="x", mode="autopilot", model=BIG, candidate_models=["evil-model"]), ALLOWED)


def test_policy_hash_ignores_the_name_and_tracks_every_setting():
    a = auto()
    assert a.policy_hash == auto(name="other").policy_hash
    for field, value in (("min_quality", 0.5), ("max_latency_ms", 999), ("max_cost_per_call_usd", 0.5), ("model", SMALL), ("candidate_models", [SMALL]), ("evidence_strategy", "iterative")):
        assert auto(**{field: value}).policy_hash != a.policy_hash, field


# ── which failures justify a fallback ────────────────────────────────────────
class Http(Exception):
    def __init__(self, status, msg="boom"):
        super().__init__(msg)
        self.status_code = status


class APIConnectionError(Exception): ...
class APITimeoutError(Exception): ...


@pytest.mark.parametrize("exc,category", [
    (Http(429), "rate_limited"), (Http(413, "rate_limit_exceeded: tokens per minute"), "rate_limited"), (Exception("Rate limit reached"), "rate_limited"),
    (Http(500), "provider_error"), (Http(502), "provider_error"), (Http(503), "provider_error"), (Http(529), "provider_error"),
    (Http(408), "timeout"), (TimeoutError("slow"), "timeout"), (APITimeoutError("Request timed out."), "timeout"), (httpx.ReadTimeout("t"), "timeout"),
    (APIConnectionError("down"), "provider_unreachable"), (ConnectionError("reset"), "provider_unreachable"), (httpx.ConnectError("x"), "provider_unreachable"),
    (Http(404, "The model `x` has been decommissioned"), "model_unavailable"), (Http(400, "model_not_found"), "model_unavailable"),
])
def test_provider_trouble_justifies_fallback(exc, category):
    f = classify_failure(exc)
    assert f is not None and f.category == category


@pytest.mark.parametrize("exc", [
    Http(400, "bad request: messages must not be empty"), Http(401, "invalid api key"), Http(403, "forbidden"), Http(404, "not found"), Http(422, "unprocessable"),
    ValueError("bad input"), KeyError("x"), RuntimeError("our own bug"), ValidationError.from_exception_data("T", []), json.JSONDecodeError("m", "d", 0),
])
def test_our_own_errors_and_client_errors_never_trigger_fallback(exc):
    assert classify_failure(exc) is None


def test_failure_description_names_category_and_status():
    assert classify_failure(Http(429)).describe() == "rate limited (HTTP 429)"
    assert classify_failure(TimeoutError()).describe() == "timeout"


# ── evidence from AtlasBench runs ────────────────────────────────────────────
def run_row(model="m", *, quality=0.9, calls=8, llm_ms=8000, ptok=12000, ctok=1600, reported=True, cases=8, strategy="single_pass", evaluator=EVALUATOR_VERSION,
            days_ago=1, run="a", completed=True, summary=True):
    s = {"composite_incl_failures": quality, "cases_total": cases, "completion_rate": 1.0,
         "performance": {"llm_calls": calls, "llm_latency_ms": llm_ms, "prompt_tokens": ptok, "completion_tokens": ctok, "tokens_reported": reported}}
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return {"run_id": f"run_{run * 32}", "evaluator_version": evaluator, "config_json": json.dumps({"model": model, "strategy": strategy}),
            "summary_json": json.dumps(s) if summary else None, "completed_at": ts, "created_at": ts}


class FakeBench:
    def __init__(self, rows):
        self.rows = rows

    def list_runs(self, user_id, **kw):
        return self.rows, len(self.rows)


def test_evidence_uses_per_call_latency_and_tokens_and_the_conservative_quality():
    e = evidence_from_run(run_row("m", quality=0.7, calls=8, llm_ms=8000, ptok=12000, ctok=1600))
    assert (e.quality, e.mean_call_latency_ms, e.avg_prompt_tokens, e.avg_completion_tokens) == (0.7, 1000.0, 1500.0, 200.0)


def test_evidence_never_invents_figures():
    e = evidence_from_run(run_row("m", calls=0, llm_ms=0, reported=False))
    assert e.mean_call_latency_ms is None and e.avg_completion_tokens is None
    assert evidence_from_run(run_row("m", reported=False)).avg_prompt_tokens is None
    assert evidence_from_run(run_row("m", summary=False)) is None
    bad = run_row("m")
    bad["config_json"] = "not json"
    assert evidence_from_run(bad) is None


def test_load_evidence_filters_stale_foreign_small_and_wrong_strategy_runs():
    pol = auto(max_evidence_age_days=30, min_evidence_cases=4)
    rows = [run_row("fresh", run="1"), run_row("old", days_ago=60, run="2"), run_row("tiny", cases=2, run="3"), run_row("other-scorer", evaluator="atlasbench-0.9", run="4"),
            run_row("iter", strategy="iterative", run="5")]
    got = load_evidence(FakeBench(rows), "u", pol, NOW)
    assert set(got) == {"fresh", "iter"}
    only_iter = load_evidence(FakeBench(rows), "u", auto(evidence_strategy="iterative"), NOW)
    assert set(only_iter) == {"iter"}


def test_load_evidence_takes_the_newest_run_per_model():
    rows = [run_row("m", quality=0.95, run="1"), run_row("m", quality=0.20, run="2")]    # list_runs is newest-first
    assert load_evidence(FakeBench(rows), "u", auto(), NOW)["m"].quality == 0.95


# ── explicit selection ───────────────────────────────────────────────────────
def test_explicit_policy_always_uses_its_model_whatever_the_evidence_says():
    d = selector.decide(RoutingPolicy(name="fixed", model=SMALL), ALLOWED, {SMALL: ev(SMALL, quality=0.1), BIG: ev(BIG)}, 2000)
    assert (d.mode, d.requested_model, d.reason_code) == ("explicit", SMALL, "explicit") and "Explicit selection" in d.reason and d.est_cost_usd > 0


def test_estimates_are_labelled_estimates_and_unknown_when_unpriced():
    assert selector.estimate_prompt_tokens(4000) == 1000 and selector.estimate_prompt_tokens(0) == 1
    d = selector.decide(RoutingPolicy(name="x", model="private-model"), {**ALLOWED, "private-model": {"label": "p", "price_in": None, "price_out": None}}, {}, 1000)
    assert d.est_cost_usd is None


# ── autopilot ────────────────────────────────────────────────────────────────
EVID = {BIG: ev(BIG, quality=0.95, latency=1200), MID: ev(MID, quality=0.88, latency=800), SMALL: ev(SMALL, quality=0.55, latency=300)}


def test_autopilot_picks_the_cheapest_model_that_meets_every_requirement():
    d = selector.decide(auto(min_quality=0.8), ALLOWED, EVID, 2000)
    assert d.requested_model == MID and d.reason_code == "autopilot_selected" and d.evidence_run_id == EVID[MID].run_id
    assert "cheapest of 2 qualifying" in d.reason and f"run_{'a' * 8}" in d.reason
    by = {c.model: c for c in d.candidates}
    assert by[BIG].eligible and by[MID].eligible and not by[SMALL].eligible and by[MID].est_cost_usd < by[BIG].est_cost_usd
    assert "quality 0.55 < required 0.80" in by[SMALL].rejected[0]
    assert "Cheaper but rejected: llama-3.1-8b-instant" in d.reason         # the explanation names the cheaper model it refused, and why


def test_the_quality_floor_can_force_the_expensive_model():
    d = selector.decide(auto(min_quality=0.92), ALLOWED, EVID, 2000)
    assert d.requested_model == BIG and "cheapest of 1 qualifying" in d.reason


def test_a_lower_floor_lets_the_cheapest_model_win():
    d = selector.decide(auto(min_quality=0.5), ALLOWED, EVID, 2000)
    assert d.requested_model == SMALL


def test_latency_limit_excludes_slow_models():
    ev_ = {**EVID, MID: ev(MID, quality=0.88, latency=6500)}
    d = selector.decide(auto(min_quality=0.8, max_latency_ms=4000), ALLOWED, ev_, 2000)
    assert d.requested_model == BIG
    mid = next(c for c in d.candidates if c.model == MID)
    assert not mid.eligible and "latency 6500 ms/call > limit 4000 ms" in mid.rejected[0]


def test_cost_limit_depends_on_the_size_of_the_call():
    small_call, big_call = selector.decide(auto(max_cost_per_call_usd=0.001), ALLOWED, EVID, 500), selector.decide(auto(max_cost_per_call_usd=0.001), ALLOWED, EVID, 40_000)
    assert small_call.reason_code == "autopilot_selected"                                  # a short prompt fits the budget...
    assert big_call.reason_code == "autopilot_no_qualifying_model" and "per call > limit" in big_call.reason   # ...a huge one does not


def test_every_requirement_must_hold_at_once():
    only_slow_good = {BIG: ev(BIG, quality=0.99, latency=9000), MID: ev(MID, quality=0.5, latency=100)}
    d = selector.decide(auto(min_quality=0.8, max_latency_ms=4000), ALLOWED, only_slow_good, 2000)
    assert d.reason_code == "autopilot_no_qualifying_model" and d.requested_model == BIG       # falls to the policy default, explained
    assert "Using the default model" in d.reason and all(not c.eligible for c in d.candidates)


def test_without_any_benchmark_evidence_autopilot_says_so_and_uses_the_default():
    d = selector.decide(auto(model=SMALL), ALLOWED, {}, 2000)
    assert d.requested_model == SMALL and d.reason_code == "autopilot_no_evidence" and "no usable AtlasBench results" in d.reason
    assert all("no usable AtlasBench result" in c.rejected[0] or "estimated" in c.rejected[0] for c in d.candidates)


def test_unknown_is_never_good_enough():
    no_latency = {MID: ev(MID, quality=0.99, latency=None)}
    d = selector.decide(auto(candidate_models=[MID]), ALLOWED, no_latency, 2000)
    assert d.reason_code == "autopilot_no_qualifying_model" and "latency is unknown" in d.candidates[0].rejected[0]
    unpriced = {**ALLOWED, "private": {"label": "p", "price_in": None, "price_out": None}}
    d = selector.decide(auto(candidate_models=["private"]), unpriced, {"private": ev("private", quality=0.99)}, 2000)
    assert not d.candidates[0].eligible and "price unknown" in d.candidates[0].rejected[0]


def test_the_candidate_pool_restricts_the_choice():
    d = selector.decide(auto(min_quality=0.5, candidate_models=[BIG, MID]), ALLOWED, EVID, 2000)
    assert d.requested_model == MID and {c.model for c in d.candidates} == {BIG, MID}


def test_autopilot_is_deterministic_and_breaks_ties_by_quality_then_latency_then_name():
    same_price = {m: {"label": m, "price_in": 1.0, "price_out": 1.0} for m in ("a-model", "b-model", "c-model")}
    evs = {"a-model": ev("a-model", quality=0.85, latency=900), "b-model": ev("b-model", quality=0.95, latency=900), "c-model": ev("c-model", quality=0.95, latency=500)}
    pol = auto(model="a-model", candidate_models=["a-model", "b-model", "c-model"])
    picks = {selector.decide(pol, same_price, evs, 2000).requested_model for _ in range(5)}
    assert picks == {"c-model"}                                                        # best quality, then fastest


def test_the_estimated_completion_comes_from_the_benchmark_when_known():
    chatty, terse = ev(BIG, completion=4000.0), ev(BIG, completion=50.0)
    a = selector.est_call_cost(BIG, ALLOWED, 1000, chatty)
    b = selector.est_call_cost(BIG, ALLOWED, 1000, terse)
    c = selector.est_call_cost(BIG, ALLOWED, 1000, None)
    assert a > c > b
