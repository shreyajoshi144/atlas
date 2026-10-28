"""M9 — the router: explicit and autopilot selection, fallback, cost accounting, benchmark pinning,
isolation. Only the provider clients are fake; policies, usage rows and evidence run against the real DB."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.bench.metering import estimate_cost_usd
from backend.core.llm import invoke_with_retry
from backend.routing import repository as repo_mod
from backend.routing.context import current_scope, routing_scope
from backend.routing.evidence import ModelEvidence
from backend.routing.repository import RoutingRepository
from backend.routing.router import ModelRouter, RoutedLLM
from backend.routing.service import RoutingService
from backend.tests.m6_fixtures import NOW, _fake_embeddings, make_user, repository  # noqa: F401  (pytest fixtures)

BIG, SMALL, MID, HUGE = "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b"
_sleep = time.sleep
MSG = [HumanMessage(content="x" * 4000)]                    # ≈ 1000 estimated prompt tokens


class Http(Exception):
    def __init__(self, status, msg="provider said no"):
        super().__init__(msg)
        self.status_code = status


class APIConnectionError(Exception): ...


def ok(p=1000, c=200):
    return ("ok", p, c)


class FakeClient:
    """Plays a script per call; the last step repeats. A step is an exception, ``("ok", prompt, completion)`` or ``"ok-no-usage"``."""

    def __init__(self, model, script, calls):
        self.model, self.script, self.calls = model, list(script), calls
        self.model_name = model

    def _next(self):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.calls.append(self.model)
        return step

    def invoke(self, messages, *a, **kw):
        step = self._next()
        if isinstance(step, BaseException):
            raise step
        _sleep(0.002)                      # a real (if tiny) latency, immune to tests that patch time.sleep
        if step == "ok-no-usage":
            return AIMessage(content=f"answer from {self.model}")
        _, p, c = step
        return AIMessage(content=f"answer from {self.model}", usage_metadata={"input_tokens": p, "output_tokens": c, "total_tokens": p + c})

    def with_structured_output(self, schema, **kw):
        outer = self

        class Structured:
            def invoke(self_inner, messages, *a, **kw2):
                step = outer._next()
                if isinstance(step, BaseException):
                    raise step
                return SimpleNamespace(answer=f"structured from {outer.model}")
        return Structured()


class Harness:
    def __init__(self, repository, scripts=None, default_model=BIG):
        self.repository = repository
        self.calls: list[str] = []
        self.scripts = scripts or {}
        self.router = ModelRouter(repository_factory=lambda: repository, client_factory=lambda m: FakeClient(m, self.scripts.get(m, [ok()]), self.calls),
                                  clock=lambda: NOW, default_model=default_model)
        self.llm = RoutedLLM(self.router)
        self.service = RoutingService(repository, router=self.router, clock=lambda: NOW)
        self.usage = RoutingRepository(repository.db)

    def policy(self, user, active=True, **kw):
        base = {"name": f"p{len(self.service.list_policies(user)['items'])}", "model": BIG}
        p = self.service.create_policy(user, {**base, **kw})
        if active:
            self.service.set_active(user, p["policy_id"])
        return p

    def rows(self, user, **kw):
        return self.service.list_usage(user, limit=100, **kw)["items"]


@pytest.fixture()
def h(repository):
    return Harness(repository)


@pytest.fixture()
def alice(repository):
    return make_user(repository, "Alice")


@pytest.fixture()
def bob(repository):
    return make_user(repository, "Bob")


def ask(h, user, purpose="research"):
    with routing_scope(user, purpose):
        return h.llm.invoke(MSG)


# ── no scope / no policy ─────────────────────────────────────────────────────
def test_calls_with_no_scope_are_legacy_passthrough_and_leave_no_trace(h, alice):
    h.policy(alice, model=SMALL)                                     # a policy exists, but nobody asked on this user's behalf
    out = h.llm.invoke(MSG)
    assert out.content == f"answer from {BIG}" and h.calls == [BIG] and h.rows(alice) == []


def test_without_a_policy_the_application_model_answers_and_the_call_is_still_recorded(h, alice):
    out = ask(h, alice)
    assert out.content == f"answer from {BIG}"
    (r,) = h.rows(alice)
    assert (r["requested_model"], r["actual_model"], r["status"], r["mode"], r["policy_id"], r["policy_name"]) == (BIG, BIG, "ok", "explicit", None, "Application default")
    assert r["reason_code"] == "explicit" and BIG in r["reason"] and r["fallback_used"] is False and r["fallback_reason"] is None
    assert r["purpose"] == "research" and r["latency_ms"] >= 2 and r["llm_latency_ms"] >= 2 and r["attempts"] == [{**r["attempts"][0], "model": BIG, "outcome": "ok"}]


def test_explicit_policy_selects_its_model(h, alice):
    p = h.policy(alice, model=MID)
    out = ask(h, alice)
    (r,) = h.rows(alice)
    assert out.content == f"answer from {MID}" and h.calls == [MID] and r["requested_model"] == r["actual_model"] == MID and r["policy_id"] == p["policy_id"]


def test_deactivating_a_policy_returns_to_the_application_default(h, alice):
    p = h.policy(alice, model=MID)
    ask(h, alice)
    h.service.set_active(alice, None)
    ask(h, alice)
    assert h.calls == [MID, BIG] and [r["policy_id"] for r in h.rows(alice)] == [None, p["policy_id"]]


# ── autopilot ────────────────────────────────────────────────────────────────
def evid(model, quality, latency=800.0):
    return ModelEvidence(model=model, run_id=f"run_{model[:1] * 32}", completed_at="2026-10-05T00:00:00+00:00", strategy="single_pass", quality=quality, cases=8,
                         completion_rate=1.0, mean_call_latency_ms=latency, avg_prompt_tokens=1000.0, avg_completion_tokens=200.0)


def test_autopilot_routes_to_the_cheapest_qualifying_model_and_records_why(h, alice, monkeypatch):
    monkeypatch.setattr("backend.routing.router.load_evidence", lambda *a, **k: {BIG: evid(BIG, 0.95), MID: evid(MID, 0.9), SMALL: evid(SMALL, 0.4)})
    h.policy(alice, mode="autopilot", min_quality=0.85, max_latency_ms=4000, max_cost_per_call_usd=0.05)
    out = ask(h, alice)
    (r,) = h.rows(alice)
    assert out.content == f"answer from {MID}" and r["requested_model"] == MID and r["mode"] == "autopilot" and r["reason_code"] == "autopilot_selected"
    assert "cheapest of 2 qualifying" in r["reason"] and r["evidence_run_id"] == evid(MID, 0.9).run_id and r["est_cost_usd"] > 0


def test_autopilot_without_evidence_uses_the_default_and_says_so(h, alice):
    h.policy(alice, mode="autopilot", model=SMALL)
    ask(h, alice)
    (r,) = h.rows(alice)
    assert r["actual_model"] == SMALL and r["reason_code"] == "autopilot_no_evidence" and "no usable AtlasBench results" in r["reason"]


def test_autopilot_decision_follows_the_size_of_each_call(h, alice, monkeypatch):
    monkeypatch.setattr("backend.routing.router.load_evidence", lambda *a, **k: {BIG: evid(BIG, 0.95), MID: evid(MID, 0.9)})
    h.policy(alice, mode="autopilot", model=SMALL, min_quality=0.8, max_cost_per_call_usd=0.0004)
    with routing_scope(alice, "research"):
        h.llm.invoke([HumanMessage(content="y" * 400)])                  # tiny → fits the budget on the cheaper model
        h.llm.invoke([HumanMessage(content="y" * 400_000)])              # huge → nothing fits → explicit default, explained
    big, small = h.rows(alice)[0], h.rows(alice)[1]
    assert small["actual_model"] == MID and big["reason_code"] == "autopilot_no_qualifying_model" and big["actual_model"] == SMALL


def test_a_policy_model_that_is_no_longer_allowed_falls_back_to_the_application_default(repository, alice):
    h1 = Harness(repository, default_model="private-model")
    h1.policy(alice, model="private-model")
    h2 = Harness(repository, default_model=BIG)                          # the application default changed; "private-model" is gone
    with routing_scope(alice, "research"):
        h2.llm.invoke(MSG)
    r = h2.rows(alice)[0]
    assert r["actual_model"] == BIG and r["reason_code"] == "model_not_allowed" and "no longer allowed" in r["reason"]


# ── fallback ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("failure,label", [
    (Http(429), "rate limited (HTTP 429)"), (Http(500), "provider error (HTTP 500)"), (Http(503), "provider error (HTTP 503)"),
    (TimeoutError("slow"), "timeout"), (httpx.ReadTimeout("slow"), "timeout"), (APIConnectionError("down"), "provider unreachable"),
    (Http(404, "model_decommissioned"), "model unavailable (HTTP 404)"),
])
def test_provider_failures_fall_back_and_the_fallback_is_recorded(repository, alice, failure, label, caplog):
    h = Harness(repository, {BIG: [failure], SMALL: [ok(1000, 200)]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    with caplog.at_level(logging.WARNING):
        out = ask(h, alice)
    (r,) = h.rows(alice)
    assert out.content == f"answer from {SMALL}" and h.calls == [BIG, SMALL]
    assert (r["requested_model"], r["actual_model"], r["status"], r["fallback_used"]) == (BIG, SMALL, "ok", True)
    assert r["fallback_reason"] == f"{BIG}: {label} → {SMALL}" and [a["outcome"] for a in r["attempts"]] == ["failed", "ok"]
    assert [a["model"] for a in r["attempts"]] == [BIG, SMALL] and r["attempts"][0]["category"] in {"rate_limited", "provider_error", "timeout", "provider_unreachable", "model_unavailable"}
    assert any(rec.message == "Model fallback" and rec.reason == label for rec in caplog.records)          # never silent: it is logged too


def test_fallback_walks_the_chain_in_order(repository, alice):
    h = Harness(repository, {BIG: [Http(429)], SMALL: [Http(503)], MID: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL, MID])
    out = ask(h, alice)
    (r,) = h.rows(alice)
    assert out.content == f"answer from {MID}" and h.calls == [BIG, SMALL, MID]
    assert r["actual_model"] == MID and len(r["attempts"]) == 3 and r["fallback_reason"].count("→") == 2


def test_an_exhausted_chain_raises_the_original_error_and_records_the_failure(repository, alice):
    last = Http(503, "all down")
    h = Harness(repository, {BIG: [Http(429)], SMALL: [last]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    with pytest.raises(Http) as e:
        ask(h, alice)
    assert e.value is last
    (r,) = h.rows(alice)
    assert (r["status"], r["actual_model"], r["fallback_used"]) == ("failed", None, False) and "Fallback exhausted" in r["fallback_reason"]
    assert r["error_type"] == "Http" and r["actual_cost_usd"] is None and r["total_tokens"] is None and len(r["attempts"]) == 2


@pytest.mark.parametrize("failure", [Http(400, "bad request"), Http(401, "invalid api key"), Http(403, "forbidden"), ValueError("our bug"), KeyError("x")])
def test_errors_that_are_not_provider_trouble_never_fall_back(repository, alice, failure):
    h = Harness(repository, {BIG: [failure], SMALL: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    with pytest.raises(type(failure)):
        ask(h, alice)
    (r,) = h.rows(alice)
    assert h.calls == [BIG] and r["status"] == "failed" and r["fallback_used"] is False and r["fallback_reason"] is None
    assert r["attempts"][0]["category"] == "not_eligible_for_fallback"


def test_fallback_disabled_raises_the_provider_error_untouched(repository, alice):
    err = Http(429, "slow down")
    h = Harness(repository, {BIG: [err], SMALL: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=False)
    with pytest.raises(Http) as e:
        ask(h, alice)
    assert e.value is err and h.calls == [BIG]
    (r,) = h.rows(alice)
    assert r["status"] == "failed" and r["fallback_reason"] == "Fallback is disabled in this policy."


def test_default_behaviour_with_no_policy_never_falls_back(repository, alice):
    h = Harness(repository, {BIG: [Http(500)], SMALL: [ok()]})
    with pytest.raises(Http):
        ask(h, alice)
    assert h.calls == [BIG]


def test_a_healthy_primary_never_touches_the_fallback_models(repository, alice):
    h = Harness(repository, {BIG: [ok()], SMALL: [Http(500)]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    ask(h, alice)
    assert h.calls == [BIG] and h.rows(alice)[0]["fallback_used"] is False and h.rows(alice)[0]["fallback_reason"] is None


def test_autopilot_chooses_first_and_fallback_still_applies_to_its_choice(repository, alice, monkeypatch):
    monkeypatch.setattr("backend.routing.router.load_evidence", lambda *a, **k: {MID: evid(MID, 0.9), BIG: evid(BIG, 0.95)})
    h = Harness(repository, {MID: [Http(429)], BIG: [ok()]})
    h.policy(alice, mode="autopilot", model=BIG, min_quality=0.85, fallback_enabled=True, fallback_models=[SMALL])
    out = ask(h, alice)
    r = h.rows(alice)[0]
    assert h.calls[0] == MID and r["requested_model"] == MID and r["mode"] == "autopilot" and r["fallback_used"] is True and r["actual_model"] == SMALL
    assert out.content == f"answer from {SMALL}"


# ── cost accounting ──────────────────────────────────────────────────────────
def test_actual_cost_uses_the_answering_models_price_and_the_reported_tokens(h, alice):
    h.policy(alice, model=BIG)
    ask(h, alice)
    (r,) = h.rows(alice)
    assert (r["prompt_tokens"], r["completion_tokens"], r["total_tokens"], r["tokens_reported"]) == (1000, 200, 1200, True)
    assert r["actual_cost_usd"] == pytest.approx(estimate_cost_usd(BIG, 1000, 200)) == pytest.approx((1000 * 0.59 + 200 * 0.79) / 1e6)
    assert r["est_cost_usd"] is not None and r["est_cost_usd"] != r["actual_cost_usd"]       # an estimate made BEFORE the call, kept separate


def test_after_a_fallback_the_cost_is_the_fallback_models_not_the_requested_ones(repository, alice):
    h = Harness(repository, {BIG: [Http(429)], SMALL: [ok(2000, 100)]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    ask(h, alice)
    (r,) = h.rows(alice)
    assert r["actual_cost_usd"] == pytest.approx((2000 * 0.05 + 100 * 0.08) / 1e6) and r["prompt_tokens"] == 2000
    assert r["est_cost_usd"] == pytest.approx(estimate_cost_usd(BIG, 1000, 600))            # the estimate was for the model that was REQUESTED


def test_unreported_tokens_and_unpriced_models_are_unknown_not_zero(repository, alice):
    h = Harness(repository, {BIG: ["ok-no-usage"]})
    ask(h, alice)
    r = h.rows(alice)[0]
    assert r["tokens_reported"] is False and r["total_tokens"] is None and r["actual_cost_usd"] is None
    hp = Harness(repository, {"private-model": [ok()]}, default_model="private-model")
    with routing_scope(alice, "chat"):
        hp.llm.invoke(MSG)
    p = hp.rows(alice)[0]
    assert p["total_tokens"] == 1200 and p["actual_cost_usd"] is None and p["est_cost_usd"] is None      # tokens known, price unknown


def test_usage_summary_totals_are_exact_and_honest_about_gaps(repository, alice):
    h = Harness(repository, {BIG: [ok(1000, 200), ok(500, 100), "ok-no-usage", Http(500)]})
    h.policy(alice, model=BIG)
    for _ in range(3):
        ask(h, alice)
    with pytest.raises(Http):
        ask(h, alice)
    s = h.service.usage_summary(alice, 30)
    m = s["by_model"][0]
    assert m["model"] == BIG and m["calls"] == 4 and m["failed"] == 1 and m["fallbacks"] == 0
    assert m["total_tokens"] == 1800 and m["cost_usd"] == pytest.approx((1500 * 0.59 + 300 * 0.79) / 1e6)
    assert m["cost_complete"] is False and m["tokens_complete"] is False                      # one successful call reported nothing
    assert s["total"]["calls"] == 4 and s["total"]["failed"] == 1 and s["total"]["fallback_rate"] == 0.0 and s["total"]["cost_complete"] is False


# ── AtlasBench stays valid ───────────────────────────────────────────────────
def test_benchmark_scope_pins_the_model_and_forbids_fallback_and_autopilot(repository, alice, monkeypatch):
    monkeypatch.setattr("backend.routing.router.load_evidence", lambda *a, **k: {MID: evid(MID, 0.99)})
    h = Harness(repository, {SMALL: [Http(429)], MID: [ok()], BIG: [ok()]})
    h.policy(alice, mode="autopilot", model=BIG, fallback_enabled=True, fallback_models=[MID], min_quality=0.5)
    with routing_scope(alice, "benchmark", benchmark=True, model=SMALL):
        with pytest.raises(Http):
            h.llm.invoke(MSG)
    assert h.calls == [SMALL]                                       # not the autopilot's pick, and no fallback to MID
    assert h.rows(alice) == []                                      # benchmark calls are not production usage


def test_benchmark_scope_with_an_unlisted_model_fails_without_substitution(h, alice):
    with routing_scope(alice, "benchmark", benchmark=True, model="not-allowed"):
        with pytest.raises(ValueError, match="refusing to substitute"):
            h.llm.invoke(MSG)
    assert h.calls == []


def test_daily_budget_event_counts_routed_fallback_chain_once(h, alice):
    from backend.ops.repository import OperationalRepository, UsageLimitExceeded

    OperationalRepository(h.repository.db).set_limits(
        alice, daily_call_limit=1, monthly_spend_cap_usd=None, actor_id=alice, now=NOW)
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    h.scripts.update({BIG: [Http(503)], SMALL: [ok()]})
    with routing_scope(alice, "research"):
        answer = h.llm.invoke(MSG)
    assert answer.content == f"answer from {SMALL}" and h.calls == [BIG, SMALL]
    with h.repository.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_budget_events WHERE user_id=?", (alice,)).fetchone()[0] == 1
    with routing_scope(alice, "research"), pytest.raises(UsageLimitExceeded, match="Daily"):
        h.llm.invoke(MSG)
    assert h.calls == [BIG, SMALL]


def test_an_atlasbench_run_is_unaffected_by_an_active_fallback_policy(repository, alice, monkeypatch):
    """End to end: the real benchmark service, a user whose production policy has fallback enabled, and a failing model."""
    from backend.bench.service import BenchService
    from backend.tests.bench_fixtures import ScriptedLLM

    monkeypatch.setattr("backend.core.llm.time.sleep", lambda s: None)       # the verifier's existing 429 backoff, not under test
    calls: list[str] = []

    class Flaky(ScriptedLLM):
        def invoke(self, messages, *a, **kw):
            calls.append("bench-model")
            raise Http(429, "rate limited")

    h = Harness(repository, {MID: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[MID])
    bs = BenchService(repository, llm_factory=lambda c: Flaky("perfect"), clock=lambda: NOW)
    run = bs.create_run(alice, "preset-baseline", task_ids=["core-clean-support"])
    view, _ = bs.execute_run(alice, run["run_id"])
    assert view["status"] == "failed" and view["cases_failed"] == 1 and h.calls == [] and len(calls) == 3      # the existing retry loop, no model switch        # the failure is MEASURED, not hidden by a fallback
    assert h.rows(alice) == []


# ── resilience ───────────────────────────────────────────────────────────────
def test_a_failure_to_record_usage_never_breaks_the_call_but_is_never_silent(h, alice, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(RoutingRepository, "insert_usage", boom)
    with caplog.at_level(logging.WARNING):
        out = ask(h, alice)
    assert out.content == f"answer from {BIG}" and any("NOT recorded" in r.message for r in caplog.records)


def test_if_routing_itself_breaks_the_application_model_answers_and_the_reason_says_so(h, alice, monkeypatch):
    monkeypatch.setattr(h.router, "active_policy", lambda uid: (_ for _ in ()).throw(RuntimeError("db locked")))
    out = ask(h, alice)
    r = h.rows(alice)[0]
    assert out.content == f"answer from {BIG}" and r["reason_code"] == "routing_unavailable" and "RuntimeError" in r["reason"]


def test_structured_output_is_routed_recorded_and_falls_back_too(repository, alice):
    h = Harness(repository, {BIG: [Http(500)], SMALL: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    structured = h.llm.with_structured_output(dict)
    with routing_scope(alice, "chat"):
        out = structured.invoke(MSG)
    r = h.rows(alice)[0]
    assert out.answer == f"structured from {SMALL}" and r["actual_model"] == SMALL and r["fallback_used"] is True and r["purpose"] == "chat"
    assert r["tokens_reported"] is False and r["actual_cost_usd"] is None                      # structured output carries no usage metadata


def test_attributes_of_the_application_model_stay_reachable(h):
    assert h.llm.model_name == BIG
    with pytest.raises(AttributeError):
        h.llm._private_thing


# ── invoke_with_retry (unchanged) still works on top of the router ───────────
def test_a_rate_limit_with_fallback_is_absorbed_before_any_retry_sleep(repository, alice, monkeypatch):
    h = Harness(repository, {BIG: [Http(429)], SMALL: [ok()]})
    h.policy(alice, model=BIG, fallback_enabled=True, fallback_models=[SMALL])
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("retry sleep happened")))
    with routing_scope(alice, "research"):
        out = invoke_with_retry(h.llm, MSG)
    assert out.content == f"answer from {SMALL}"


def test_without_fallback_the_existing_retry_loop_still_retries_and_each_try_is_recorded(repository, alice, monkeypatch):
    h = Harness(repository, {BIG: [Http(429), ok()]})
    monkeypatch.setattr("backend.core.llm.time.sleep", lambda s: None)
    with routing_scope(alice, "research"):
        out = invoke_with_retry(h.llm, MSG)
    rows = h.rows(alice)
    assert out.content == f"answer from {BIG}" and [r["status"] for r in rows] == ["ok", "failed"] and h.calls == [BIG, BIG]


# ── grouping, isolation, retention ───────────────────────────────────────────
def test_calls_of_one_scope_share_a_run_ref_and_scopes_do_not(h, alice):
    with routing_scope(alice, "research"):
        h.llm.invoke(MSG)
        h.llm.invoke(MSG)
    with routing_scope(alice, "chat"):
        h.llm.invoke(MSG)
    chat, r2, r1 = h.rows(alice)
    assert r1["run_ref"] == r2["run_ref"] != chat["run_ref"] and (r1["purpose"], chat["purpose"]) == ("research", "chat")
    assert len(h.service.list_usage(alice, run_ref=r1["run_ref"])["items"]) == 2 and current_scope() is None      # the scope is released


def test_policies_and_usage_never_cross_users(repository, alice, bob):
    h = Harness(repository, {BIG: [ok()], SMALL: [ok()]})
    h.policy(alice, model=SMALL)
    ask(h, alice)
    ask(h, bob)
    assert h.calls == [SMALL, BIG]                                                             # Bob is untouched by Alice's policy
    assert [r["actual_model"] for r in h.rows(alice)] == [SMALL] and [r["actual_model"] for r in h.rows(bob)] == [BIG]
    assert h.service.usage_summary(bob)["total"]["calls"] == 1 and h.service.list_policies(bob)["items"] == []


def test_usage_retention_is_capped_per_user_and_prunes_oldest_first(repository, alice, bob, monkeypatch):
    monkeypatch.setattr(repo_mod, "MAX_USAGE_ROWS_PER_USER", 5)
    h = Harness(repository)
    ask(h, bob)
    for _ in range(9):
        ask(h, alice)
    assert h.service.list_usage(alice)["total"] == 5 and h.service.list_usage(bob)["total"] == 1
    ids_ = [r["usage_id"] for r in h.rows(alice)]
    assert ids_ == sorted(ids_, reverse=True) and len(ids_) == 5
