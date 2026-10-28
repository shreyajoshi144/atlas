from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from langchain_core.messages import AIMessage

from backend.bench.arbitration import ArbitrationInputError, arbitrate_candidates
from backend.bench.models import COMPOSITE_WEIGHTS, EVALUATOR_VERSION, allowed_models
from backend.database import SQLiteManager
from backend.ops.repository import OperationalRepository, UsageLimitExceeded
from backend.tests.bench_fixtures import by_model
from backend.tests.m7_fixtures import NOW, make_user, repository  # noqa: F401


def _candidate(run_id, model, *, task_hash="a" * 64, correctness=0.5, grounding=0.5, output=None):
    return {"run_id": run_id, "model": model, "task_id": "task", "task_hash": task_hash,
            "evaluator_version": EVALUATOR_VERSION, "status": "completed",
            "composite": correctness, "scores": {"claim_correctness": correctness, "evidence_grounding": grounding,
            "citation_accuracy": correctness, "evidence_coverage": correctness, "contradiction_handling": correctness},
            "output": output or {"claims": [{"claim": "stored evidence-backed result"}]}}


def test_arbitration_uses_evidence_scores_and_stable_selection():
    strong = _candidate("run_a", "model-a", correctness=0.9, grounding=0.8)
    weak = _candidate("run_b", "model-b", correctness=0.7, grounding=0.2)
    result = arbitrate_candidates([weak, strong])
    assert result["selected_run_id"] == "run_a"
    assert result["selected_output"] == strong["output"]
    assert "same frozen evidence" in result["rationale"][0]
    assert "no unsupported synthesis" in result["rationale"][-1]
    assert arbitrate_candidates([strong, weak]) == result


@pytest.mark.parametrize("candidates", [
    [_candidate("run_a", "a")],
    [_candidate("run_a", "a"), _candidate("run_b", "b", task_hash="b" * 64)],
    [_candidate("run_a", "a"), _candidate("run_b", "a")],
])
def test_arbitration_rejects_incomparable_candidates(candidates):
    with pytest.raises(ArbitrationInputError):
        arbitrate_candidates(candidates)


def test_arbitration_rejects_one_success_plus_one_failed_candidate():
    failed = {**_candidate("run_b", "model-b"), "status": "failed", "output": None, "error_type": "provider_error"}
    good = _candidate("run_a", "model-a", correctness=0.8, grounding=0.9)
    with pytest.raises(ArbitrationInputError, match="at least two completed outputs"):
        arbitrate_candidates([failed, good])


def test_arbitration_ignores_failed_models_for_diversity_but_requires_two_eligible_models():
    failed = {**_candidate("run_f", "model-b"), "status": "failed", "output": None}
    one_good = _candidate("run_a", "model-a")
    with pytest.raises(ArbitrationInputError, match="two completed outputs"):
        arbitrate_candidates([one_good, failed, {**failed, "run_id": "run_f2"}])
    same_model = _candidate("run_c", "model-a")
    with pytest.raises(ArbitrationInputError, match="distinct identified models"):
        arbitrate_candidates([one_good, same_model, failed])


@pytest.mark.parametrize("change", [
    lambda candidates: candidates[1].update(evaluator_version="atlasbench-old"),
    lambda candidates: candidates[1].update(task_hash="b" * 64),
    lambda candidates: candidates[1].update(task_id="other-task"),
    lambda candidates: candidates[1].update(evidence_hash="c" * 64),
])
def test_arbitration_rejects_different_evaluator_or_frozen_evidence(change):
    candidates = [_candidate("run_a", "model-a"), _candidate("run_b", "model-b")]
    change(candidates)
    with pytest.raises(ArbitrationInputError):
        arbitrate_candidates(candidates)


def test_arbitration_uses_weighted_composite_instead_of_correctness_first():
    high_correctness = _candidate("run_a", "model-a", correctness=1.0, grounding=0.0)
    stronger_composite = _candidate("run_b", "model-b", correctness=0.2, grounding=1.0)
    # The other shared metrics follow grounding for this deliberately stark case.
    for key in ("citation_accuracy", "evidence_coverage", "contradiction_handling"):
        high_correctness["scores"][key] = 0.0
        stronger_composite["scores"][key] = 1.0
    assert arbitrate_candidates([high_correctness, stronger_composite])["selected_run_id"] == "run_b"


def test_arbitration_rejects_when_all_candidate_scores_are_missing():
    candidates = [_candidate("run_a", "model-a"), _candidate("run_b", "model-b")]
    for candidate in candidates:
        candidate["scores"] = {key: None for key in candidate["scores"]}
        candidate["composite"] = None
    with pytest.raises(ArbitrationInputError, match="No meaningful AtlasBench score"):
        arbitrate_candidates(candidates)


def test_arbitration_rejects_insufficient_shared_metric_coverage():
    candidates = [_candidate("run_a", "model-a"), _candidate("run_b", "model-b")]
    for candidate in candidates:
        candidate["scores"] = {key: None for key in candidate["scores"]}
        candidate["scores"].update(claim_correctness=0.8, citation_accuracy=0.7)
    with pytest.raises(ArbitrationInputError, match="at least 3 shared metrics and 50%"):
        arbitrate_candidates(candidates)

    # Three shared low-weight metrics still do not meet the 50% threshold.
    for candidate in candidates:
        candidate["scores"] = {key: None for key in candidate["scores"]}
        candidate["scores"].update(claim_correctness=0.8, contradiction_handling=0.7,
                                    trajectory_quality=0.6)
    with pytest.raises(ArbitrationInputError, match="3 metrics covering 45%"):
        arbitrate_candidates(candidates)


def test_arbitration_excludes_not_applicable_metrics_for_all_candidates():
    candidates = [_candidate("run_a", "model-a", correctness=0.8, grounding=0.9),
                  _candidate("run_b", "model-b", correctness=0.6, grounding=0.7)]
    candidates[0]["scores"]["contradiction_handling"] = None
    result = arbitrate_candidates(candidates)
    assert "contradiction_handling" not in result["comparable_metrics"]
    assert set(result["candidate_composite_scores"]) == {"run_a", "run_b"}


def test_arbitration_rejects_indistinguishable_top_scores():
    candidates = [_candidate("run_a", "model-a", correctness=0.8),
                  _candidate("run_b", "model-b", correctness=0.8)]
    with pytest.raises(ArbitrationInputError, match="indistinguishable"):
        arbitrate_candidates(candidates)


def test_arbitration_treats_scores_equal_at_persisted_four_decimal_precision_as_tied():
    candidates = [_candidate("run_a", "model-a", correctness=0.8),
                  _candidate("run_b", "model-b", correctness=0.80001)]
    for candidate in candidates:
        candidate["scores"].update(evidence_grounding=0.5, citation_accuracy=0.5,
                                   evidence_coverage=0.5, contradiction_handling=0.5)
    def persisted_score(candidate):
        present = {key: value for key, value in candidate["scores"].items() if value is not None}
        weight = sum(COMPOSITE_WEIGHTS[key] for key in present)
        return round(sum(present[key] * COMPOSITE_WEIGHTS[key] for key in present) / weight, 4)
    assert persisted_score(candidates[0]) == persisted_score(candidates[1])
    with pytest.raises(ArbitrationInputError, match="indistinguishable"):
        arbitrate_candidates(candidates)


def test_budget_reservations_enforce_daily_limit_and_monthly_spend(repository):
    user = make_user(repository, "Budget")
    ops = OperationalRepository(repository.db)
    reservation = ops.reserve_call(user, "research", "llama", 0.0006,
                                   default_calls=1, default_spend=0.001, now=NOW)
    with pytest.raises(UsageLimitExceeded, match="Daily"):
        ops.reserve_call(user, "chat", "llama", 0.0001, default_calls=1, default_spend=0.001, now=NOW)
    ops.finish_call(reservation, actual_cost=0.0008, status="ok", latency_ms=12,
                    actual_model="llama", prompt_tokens=100, completion_tokens=50, now=NOW)
    # Actual spend above the reservation is charged at the greater value.
    with repository.db.get_connection() as conn:
        actual = conn.execute("SELECT actual_cost_usd,estimated_cost_usd FROM llm_budget_events WHERE event_id=?", (reservation,)).fetchone()
    assert max(actual["actual_cost_usd"], actual["estimated_cost_usd"]) == pytest.approx(0.0008)
    with pytest.raises(UsageLimitExceeded, match="Monthly"):
        ops.reserve_call(user, "research", "llama", 0.0003,
                         default_calls=10, default_spend=0.001, now=NOW)


def test_expired_reservation_is_reconciled_and_keeps_monthly_spend(repository):
    user = make_user(repository, "ExpiredReservation")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "llama", 0.0007,
                             default_calls=10, default_spend=0.001, now=NOW)
    later = NOW + timedelta(minutes=31)
    with pytest.raises(UsageLimitExceeded, match="Monthly"):
        ops.reserve_call(user, "research", "llama", 0.0004,
                         default_calls=10, default_spend=0.001, now=later)
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT status,result_status,error_type,estimated_cost_usd,reserved_cost_usd FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert tuple(row) == ("completed", "failed", "ReservationExpired", 0.0007, 0.0007)
    # Later requests in the same month still include the unresolved spend.
    with pytest.raises(UsageLimitExceeded, match="Monthly"):
        ops.reserve_call(user, "research", "llama", 0.00031,
                         default_calls=10, default_spend=0.001, now=later)


def test_successful_provider_call_followed_by_process_crash_keeps_reservation(repository, monkeypatch):
    from backend.routing.context import routing_scope
    from backend.routing.router import ModelRouter

    user = make_user(repository, "CrashAfterProvider")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "10")
    from backend.core.config import get_settings
    get_settings.cache_clear()
    class Client:
        def invoke(self, *args, **kwargs):
            return AIMessage(content="provider succeeded", usage_metadata={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4})
    router = ModelRouter(repository_factory=lambda: repository, client_factory=lambda _m: Client(), clock=lambda: NOW,
                         default_model="llama-3.3-70b-versatile")
    original_finish = OperationalRepository.finish_call
    monkeypatch.setattr(OperationalRepository, "finish_call", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated crash")))
    try:
        with routing_scope(user, "research"):
            assert router.invoke("question", lambda client: client, (), {}).content == "provider succeeded"
    finally:
        monkeypatch.setattr(OperationalRepository, "finish_call", original_finish)
        get_settings.cache_clear()
    with repository.db.get_connection() as conn:
        event = conn.execute("SELECT event_id,estimated_cost_usd,reserved_cost_usd,status FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
    assert event["status"] == "reserved" and event["estimated_cost_usd"] == event["reserved_cost_usd"] > 0
    with pytest.raises(UsageLimitExceeded, match="Monthly"):
        OperationalRepository(repository.db).reserve_call(user, "research", "llama", 0.0001,
                                                            default_calls=10, default_spend=event["reserved_cost_usd"],
                                                            now=NOW + timedelta(minutes=31))
    with repository.db.get_connection() as conn:
        reconciled = conn.execute("SELECT status,result_status,error_type,estimated_cost_usd FROM llm_budget_events WHERE event_id=?", (event["event_id"],)).fetchone()
    assert tuple(reconciled) == ("completed", "failed", "ReservationExpired", event["reserved_cost_usd"])


def test_primary_success_releases_unused_fallback_reservation(repository, monkeypatch):
    from backend.core.config import get_settings
    from backend.ops.limits import prompt_representation_chars, reserved_prompt_tokens
    from backend.routing.context import routing_scope
    from backend.routing.router import ModelRouter
    from backend.routing.service import RoutingService
    from backend.ops.repository import estimate_budget_cost

    user = make_user(repository, "PrimaryOnly")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "10")
    get_settings.cache_clear()
    primary, fallback = "llama-3.3-70b-versatile", "llama-3.1-8b-instant"
    calls = []
    class Client:
        def __init__(self, model): self.model = model
        def invoke(self, *args, **kwargs):
            calls.append(self.model)
            return AIMessage(content="ok", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    router = ModelRouter(repository_factory=lambda: repository, client_factory=Client, default_model=primary)
    service = RoutingService(repository, router=router, clock=lambda: NOW)
    policy = service.create_policy(user, {"name": "explicit", "model": primary, "fallback_enabled": True, "fallback_models": [fallback]})
    service.set_active(user, policy["policy_id"])
    prompt = "question"
    try:
        with routing_scope(user, "research"):
            router.invoke(prompt, lambda client: client, (), {})
        assert calls == [primary]
        with repository.db.get_connection() as conn:
            row = conn.execute("SELECT estimated_cost_usd,reserved_cost_usd,actual_cost_usd FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
        expected = estimate_budget_cost(primary, 10, 5, router.allowed())
        assert row["reserved_cost_usd"] > row["estimated_cost_usd"]
        assert row["estimated_cost_usd"] == pytest.approx(expected)
        assert row["actual_cost_usd"] == pytest.approx(expected)
    finally:
        get_settings.cache_clear()


def test_fallback_success_without_usage_keeps_unknown_attempt_costs(repository, monkeypatch):
    from backend.core.config import get_settings
    from backend.routing.context import routing_scope
    from backend.routing.router import ModelRouter
    from backend.routing.service import RoutingService

    user = make_user(repository, "FallbackUnknownUsage")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "10")
    get_settings.cache_clear()
    primary, fallback = "llama-3.3-70b-versatile", "llama-3.1-8b-instant"
    calls = []
    class ProviderBusy(Exception): status_code = 503
    class Client:
        def __init__(self, model): self.model = model
        def invoke(self, *args, **kwargs):
            calls.append(self.model)
            if self.model == primary:
                raise ProviderBusy("busy")
            return AIMessage(content="fallback answer")
    router = ModelRouter(repository_factory=lambda: repository, client_factory=Client, default_model=primary)
    service = RoutingService(repository, router=router, clock=lambda: NOW)
    policy = service.create_policy(user, {"name": "fallback", "model": primary, "fallback_enabled": True, "fallback_models": [fallback]})
    service.set_active(user, policy["policy_id"])
    try:
        with routing_scope(user, "research"):
            assert router.invoke("question", lambda client: client, (), {}).content == "fallback answer"
        assert calls == [primary, fallback]
        with repository.db.get_connection() as conn:
            row = conn.execute("SELECT estimated_cost_usd,reserved_cost_usd,actual_cost_usd,actual_model FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
        assert row["estimated_cost_usd"] == row["reserved_cost_usd"] > 0
        assert row["actual_cost_usd"] is None and row["actual_model"] == fallback
    finally:
        get_settings.cache_clear()


def test_atlasbench_rechecks_pinned_model_before_building_provider(repository):
    from backend.bench.service import BenchService

    user = make_user(repository, "InvalidPinnedModel")
    built = []
    service = BenchService(repository, llm_factory=lambda config: built.append(config.model), clock=lambda: NOW)
    run = service.create_run(user, "preset-baseline", task_ids=["core-clean-support"])
    with repository.db.get_connection() as conn:
        config = json.loads(conn.execute("SELECT config_json FROM bench_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0])
        config["model"] = "model-no-longer-allowed"
        conn.execute("UPDATE bench_runs SET config_json=? WHERE run_id=?",
                     (json.dumps(config, sort_keys=True), run["run_id"]))
        conn.commit()
    result, ran = service.execute_run(user, run["run_id"])
    assert ran and result["status"] == "failed" and result["error_type"] == "model_not_allowed"
    assert built == []


def test_atlasbench_success_reconciles_reserved_output_capacity(repository):
    from backend.core.config import get_settings
    from backend.ops.limits import BudgetedLLM
    from backend.ops.repository import estimate_budget_cost

    user = make_user(repository, "BenchmarkSettlement")
    class Client:
        def invoke(self, *args, **kwargs):
            return AIMessage(content="ok", usage_metadata={"input_tokens": 20, "output_tokens": 4, "total_tokens": 24})
    model = "llama-3.3-70b-versatile"
    BudgetedLLM(Client(), repository.db, user_id=user, model=model, max_tokens=2048).invoke("question")
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT estimated_cost_usd,reserved_cost_usd,actual_cost_usd FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
    expected = estimate_budget_cost(model, 20, 4, allowed_models((get_settings().openai_model,)))
    assert row["reserved_cost_usd"] > row["estimated_cost_usd"]
    assert row["estimated_cost_usd"] == pytest.approx(expected)
    assert row["actual_cost_usd"] == pytest.approx(round(expected, 6))
    OperationalRepository(repository.db).set_limits(user, daily_call_limit=1,
                                                     monthly_spend_cap_usd=None, actor_id=user, now=NOW)
    with pytest.raises(UsageLimitExceeded, match="Daily"):
        BudgetedLLM(Client(), repository.db, user_id=user, model=model, max_tokens=2048).invoke("second question")


def test_budgeted_fallback_reserves_entire_possible_chain(repository, monkeypatch):
    from backend.core.config import get_settings
    from backend.ops.limits import reserved_prompt_tokens
    from backend.routing.context import RouteScope
    from backend.routing.router import ModelRouter
    from backend.ops.repository import estimate_budget_cost
    from backend.bench.models import allowed_models

    user = make_user(repository, "FallbackReservation")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "10")
    get_settings.cache_clear()
    try:
        router = ModelRouter(repository_factory=lambda: repository,
                             client_factory=lambda _model: object(), default_model="llama-3.3-70b-versatile")
        chain = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        reservation, reserved = router._reserve_budget(RouteScope(user, "research"), chain, "x" * 800)
        expected_prompt = reserved_prompt_tokens(800)
        expected = sum(estimate_budget_cost(model, expected_prompt, get_settings().openai_max_tokens,
                                            allowed_models((get_settings().openai_model,))) for model in chain)
        assert reserved == pytest.approx(expected)
        assert expected_prompt > (800 + 3) // 4
        with repository.db.get_connection() as conn:
            row = conn.execute("SELECT estimated_cost_usd,model FROM llm_budget_events WHERE event_id=?", (reservation,)).fetchone()
        assert row["estimated_cost_usd"] == pytest.approx(expected) and row["model"] == ",".join(chain)
    finally:
        get_settings.cache_clear()


def test_concurrent_budget_reservations_cannot_exceed_call_limit(repository):
    user = make_user(repository, "ConcurrentBudget")
    ops = OperationalRepository(repository.db)
    def reserve(purpose):
        try:
            return ops.reserve_call(user, purpose, "llama", 0.0,
                                    default_calls=1, default_spend=None, now=NOW)
        except UsageLimitExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("research", "chat")))
    assert sum(result is not None for result in results) == 1
    with repository.db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_budget_events WHERE user_id=? AND status='reserved'", (user,)).fetchone()[0]
    assert count == 1


def test_concurrent_budget_reservations_cannot_exceed_monthly_spend(repository):
    user = make_user(repository, "ConcurrentSpend")
    ops = OperationalRepository(repository.db)
    def reserve(purpose):
        try:
            return ops.reserve_call(user, purpose, "llama", 0.0006,
                                    default_calls=10, default_spend=0.001, now=NOW)
        except UsageLimitExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("research", "chat")))
    assert sum(result is not None for result in results) == 1
    with repository.db.get_connection() as conn:
        total = conn.execute("SELECT SUM(estimated_cost_usd) FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()[0]
    assert total == pytest.approx(0.0006)


def test_configured_spend_cap_fails_closed_for_unpriced_model(repository):
    user = make_user(repository, "UnpricedCap")
    with pytest.raises(UsageLimitExceeded, match="cannot be safely estimated"):
        OperationalRepository(repository.db).reserve_call(user, "research", "unpriced", None,
                                                           default_calls=100, default_spend=2.0, now=NOW)


def test_uncapped_unknown_cost_is_explicit_and_blocks_later_cap(repository):
    user = make_user(repository, "UnknownUncapped")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "unpriced", None,
                             default_calls=100, default_spend=None, now=NOW)
    ops.finish_call(event, actual_cost=None, status="ok", latency_ms=4, actual_model="unpriced",
                    prompt_tokens=12, completion_tokens=8, now=NOW)
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT estimated_cost_usd,cost_known,result_status FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert row["estimated_cost_usd"] == 0.0  # compatibility storage only; explicitly not a known/free charge
    assert row["cost_known"] == 0 and row["result_status"] == "ok"
    report = ops.monitoring(NOW.isoformat())
    assert report["budget_cost_usd"] is None
    assert report["unknown_cost_calls"] == 1
    with pytest.raises(UsageLimitExceeded, match="earlier model call costs are unknown"):
        ops.reserve_call(user, "research", "priced", 0.0001,
                         default_calls=100, default_spend=5.0, now=NOW + timedelta(minutes=1))


def test_uncapped_known_cost_remains_numeric(repository):
    user = make_user(repository, "KnownUncapped")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "priced", 0.002,
                             default_calls=100, default_spend=None, now=NOW)
    ops.finish_call(event, actual_cost=0.001, status="ok", latency_ms=4,
                    actual_model="priced", now=NOW)
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT cost_known,estimated_cost_usd,actual_cost_usd FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert row["cost_known"] == 1
    assert max(row["estimated_cost_usd"], row["actual_cost_usd"]) == pytest.approx(0.002)


def test_failed_unknown_attempt_remains_accounted_as_unknown(repository):
    user = make_user(repository, "UnknownFailed")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "unpriced", None,
                             default_calls=100, default_spend=None, now=NOW)
    ops.finish_call(event, actual_cost=None, status="failed", latency_ms=4,
                    actual_model="unpriced", error_type="TimeoutError", now=NOW)
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT cost_known,result_status,error_type FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert tuple(row) == (0, "failed", "TimeoutError")
    with pytest.raises(UsageLimitExceeded, match="earlier model call costs are unknown"):
        ops.reserve_call(user, "research", "priced", 0.0001,
                         default_calls=100, default_spend=5.0, now=NOW + timedelta(minutes=1))


def test_partial_known_fallback_cost_does_not_hide_unknown_attempt(repository):
    user = make_user(repository, "PartialFallbackUnknown")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "unpriced,priced", None,
                             default_calls=100, default_spend=None, now=NOW)
    # A successful fallback has a measured cost, but a preceding failed
    # attempt had unknown provider cost and no complete settled estimate.
    ops.finish_call(event, actual_cost=0.001, status="ok", latency_ms=8,
                    actual_model="priced", prompt_tokens=10, completion_tokens=10,
                    estimated_cost=None, now=NOW)
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT cost_known,actual_cost_usd FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert row["cost_known"] == 0
    assert row["actual_cost_usd"] == pytest.approx(0.001)
    assert ops.monitoring(NOW.isoformat())["budget_cost_usd"] is None


def test_expired_unknown_reservation_blocks_later_configured_cap(repository):
    user = make_user(repository, "UnknownExpired")
    ops = OperationalRepository(repository.db)
    event = ops.reserve_call(user, "research", "unpriced", None,
                             default_calls=100, default_spend=None, now=NOW)
    with pytest.raises(UsageLimitExceeded, match="earlier model call costs are unknown"):
        ops.reserve_call(user, "research", "priced", 0.0001,
                         default_calls=100, default_spend=5.0, now=NOW + timedelta(minutes=31))
    with repository.db.get_connection() as conn:
        row = conn.execute("SELECT status,result_status,error_type,cost_known FROM llm_budget_events WHERE event_id=?", (event,)).fetchone()
    assert tuple(row) == ("completed", "failed", "ReservationExpired", 0)


def test_concurrent_unknown_requests_fail_closed_under_spend_cap(repository):
    user = make_user(repository, "ConcurrentUnknownCap")
    ops = OperationalRepository(repository.db)
    def reserve(_):
        try:
            return ops.reserve_call(user, "research", "unpriced", None,
                                    default_calls=100, default_spend=5.0, now=NOW)
        except UsageLimitExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert results == [None, None]
    with repository.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()[0] == 0


def test_shared_router_blocks_before_model_invocation_when_user_limit_is_zero(repository):
    from backend.ops.repository import UsageLimitExceeded
    from backend.routing.context import routing_scope
    from backend.routing.router import ModelRouter

    user, admin = make_user(repository, "Blocked"), make_user(repository, "BudgetAdmin")
    OperationalRepository(repository.db).set_limits(user, daily_call_limit=0, monthly_spend_cap_usd=None, actor_id=admin, now=NOW)
    calls = []
    class Client:
        def invoke(self, *args, **kwargs):
            calls.append(1)
            return "should never be called"
    router = ModelRouter(repository_factory=lambda: repository, client_factory=lambda _m: Client(), default_model="llama-3.3-70b-versatile")
    with routing_scope(user, "research"), pytest.raises(UsageLimitExceeded, match="Daily"):
        router.invoke("question", lambda client: client, (), {})
    assert calls == []


def test_provider_failure_is_recorded_in_budget_and_routing_ledgers(repository):
    from backend.routing.context import routing_scope
    from backend.routing.router import ModelRouter

    user = make_user(repository, "ProviderFail")
    class Client:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")
    router = ModelRouter(repository_factory=lambda: repository, client_factory=lambda _m: Client(), default_model="llama-3.3-70b-versatile")
    with routing_scope(user, "research"), pytest.raises(RuntimeError, match="provider unavailable"):
        router.invoke("question", lambda client: client, (), {})
    with repository.db.get_connection() as conn:
        event = conn.execute("SELECT result_status,error_type FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
        usage = conn.execute("SELECT status,error_type FROM llm_usage WHERE user_id=?", (user,)).fetchone()
    assert tuple(event) == ("failed", "RuntimeError")
    assert tuple(usage) == ("failed", "RuntimeError")
    with repository.db.get_connection() as conn:
        budget = conn.execute("SELECT actual_cost_usd,estimated_cost_usd FROM llm_budget_events WHERE user_id=?", (user,)).fetchone()
    assert budget["actual_cost_usd"] is None and budget["estimated_cost_usd"] > 0


def test_budgets_are_user_scoped_and_admin_limit_changes_are_audited(repository):
    admin, alice, bob = (make_user(repository, n) for n in ("Admin", "LimitAlice", "LimitBob"))
    ops = OperationalRepository(repository.db)
    ops.set_limits(alice, daily_call_limit=0, monthly_spend_cap_usd=2.5, actor_id=admin, now=NOW)
    limits = ops.effective_limits(alice, 100, 50.0)
    assert limits["daily_call_limit"] == 0 and limits["monthly_spend_cap_usd"] == 2.5
    assert ops.effective_limits(bob, 100, 50.0)["daily_call_limit"] == 100
    events, total = ops.audit_logs()
    assert total == 1 and events[0]["actor_user_id"] == admin and events[0]["target_user_id"] == alice
    with repository.db.get_connection() as conn, pytest.raises(Exception, match="immutable"):
        conn.execute("DELETE FROM admin_audit_logs")


def test_fresh_and_legacy_database_migrations_add_role_and_operational_tables(tmp_path):
    fresh = SQLiteManager(str(tmp_path / "fresh.db"))
    fresh.initialize_database()
    with fresh.get_connection() as conn:
        assert "role" in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        expected = {"bench_arbitrations", "user_usage_limits", "llm_budget_events", "admin_audit_logs", "playground_conversations"}
        assert expected <= {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reserved_cost_usd" in {r["name"] for r in conn.execute("PRAGMA table_info(llm_budget_events)")}
        assert "cost_known" in {r["name"] for r in conn.execute("PRAGMA table_info(llm_budget_events)")}

    upgraded_path = str(tmp_path / "legacy.db")
    legacy = SQLiteManager(upgraded_path)
    with legacy.get_connection() as conn:
        conn.execute("CREATE TABLE users(user_id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password_hash TEXT NOT NULL,password_salt TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES('user_old','old@example.com','Old','$hash','salt','2020','2020')")
        conn.execute("CREATE TABLE llm_budget_events(event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,purpose TEXT NOT NULL,model TEXT NOT NULL,actual_model TEXT,estimated_cost_usd REAL NOT NULL DEFAULT 0,actual_cost_usd REAL,prompt_tokens INTEGER,completion_tokens INTEGER,result_status TEXT,error_type TEXT,latency_ms INTEGER,status TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT)")
        conn.execute("INSERT INTO llm_budget_events(event_id,user_id,purpose,model,estimated_cost_usd,status,created_at) VALUES('old_event','user_old','research','llama',0.004,'reserved','2020')")
        conn.execute("INSERT INTO llm_budget_events(event_id,user_id,purpose,model,estimated_cost_usd,status,created_at) VALUES('old_unknown','user_old','research','unpriced',0,'completed','2020')")
        conn.commit()
    legacy.initialize_database()
    with legacy.get_connection() as conn:
        row = conn.execute("SELECT user_id,role FROM users WHERE user_id='user_old'").fetchone()
        assert tuple(row) == ("user_old", "user")
        budget_cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_budget_events)")}
        assert "reserved_cost_usd" in budget_cols
        assert conn.execute("SELECT reserved_cost_usd FROM llm_budget_events WHERE event_id='old_event'").fetchone()[0] == pytest.approx(0.004)
        assert conn.execute("SELECT cost_known FROM llm_budget_events WHERE event_id='old_event'").fetchone()[0] == 1
        assert conn.execute("SELECT cost_known FROM llm_budget_events WHERE event_id='old_unknown'").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='playground_conversations'").fetchone()


@pytest.fixture()
def admin_app(repository, monkeypatch):
    import main as main_module
    from backend.auth.security import create_access_token
    from backend.bench.service import BenchService
    from backend.database import ResearchRepository
    from fastapi.testclient import TestClient

    importlib.reload(main_module)
    users = {name.lower(): make_user(repository, name) for name in ("Admin", "Ordinary", "Other")}
    with repository.db.get_connection() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE user_id=?", (users["admin"],))
        conn.commit()
    monkeypatch.setattr("backend.api.routes.atlasbench._service", lambda: BenchService(
        ResearchRepository(), llm_factory=by_model({"llama-3.3-70b-versatile": "perfect", "llama-3.1-8b-instant": "fabricating"}), clock=lambda: NOW))
    headers = {k: {"Authorization": f"Bearer {create_access_token(v, f'{k}@example.test')}"} for k, v in users.items()}
    with TestClient(main_module.app) as client:
        yield {"client": client, "headers": headers, "users": users, "repo": repository}


def test_admin_routes_require_role_and_record_limit_updates(admin_app):
    client, headers, users = admin_app["client"], admin_app["headers"], admin_app["users"]
    assert client.get("/api/admin/monitoring").status_code == 401
    assert client.get("/api/admin/monitoring", headers=headers["ordinary"]).status_code == 403
    response = client.get("/api/admin/monitoring", headers=headers["admin"])
    assert response.status_code == 200 and response.json()["success"]
    changed = client.put(f"/api/admin/users/{users['ordinary']}/limits", headers=headers["admin"],
                         json={"daily_call_limit": 8, "monthly_spend_cap_usd": 3.0})
    assert changed.status_code == 200
    assert changed.json()["data"]["daily_call_limit"] == 8
    audit = client.get("/api/admin/audit", headers=headers["admin"])
    assert audit.status_code == 200 and audit.json()["data"]["items"][0]["target_user_id"] == users["ordinary"]
    assert client.get("/api/admin/audit", headers=headers["ordinary"]).status_code == 403
    assert client.get(f"/api/admin/users/{users['other']}/limits", headers=headers["ordinary"]).status_code == 403
    assert client.put(f"/api/admin/users/{users['other']}/limits", headers=headers["ordinary"],
                      json={"daily_call_limit": 1, "monthly_spend_cap_usd": None}).status_code == 403


def test_atlasbench_arbitration_persists_provenance_and_isolation(admin_app):
    client, headers, users = admin_app["client"], admin_app["headers"], admin_app["users"]
    runs = []
    for config in ("preset-baseline", "preset-small-model"):
        created = client.post("/api/atlasbench/runs", headers=headers["ordinary"],
                              json={"config_id": config, "task_ids": ["core-clean-support"]})
        assert created.status_code == 200
        run_id = created.json()["data"]["run"]["run_id"]
        executed = client.post(f"/api/atlasbench/runs/{run_id}/execute", headers=headers["ordinary"])
        assert executed.status_code == 200 and executed.json()["data"]["run"]["status"] == "completed"
        runs.append(run_id)
    result = client.post("/api/atlasbench/arbitrations", headers=headers["ordinary"],
                         json={"task_id": "core-clean-support", "run_ids": runs})
    assert result.status_code == 200, result.text
    arbitration = result.json()["data"]["arbitration"]
    assert arbitration["decision"]["candidate_count"] == 2
    assert arbitration["decision"]["task_snapshot"]["corpus"]
    assert {c["provider"] for c in arbitration["decision"]["candidates"]} == {"groq"}
    assert all(c["task_hash"] == arbitration["task_hash"] for c in arbitration["decision"]["candidates"])
    assert len(arbitration["decision"]["candidates"][0]["model_outputs"]) > 0
    monitor = client.get("/api/admin/monitoring", headers=headers["admin"]).json()["data"]
    assert monitor["calls"] >= 2 and monitor["total_tokens"] > 0 and monitor["by_model_and_purpose"]
    assert client.get(f"/api/atlasbench/arbitrations/{arbitration['arbitration_id']}",
                      headers=headers["other"]).status_code == 404


def test_readiness_probe_reports_database_availability(admin_app):
    response = admin_app["client"].get("/ready")
    assert response.status_code == 200 and response.json() == {"status": "ready"}
