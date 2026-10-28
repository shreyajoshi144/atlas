"""M8 — AtlasBench service + persistence: lifecycle, resume/retry, leases, reproduction, isolation,
comparison, and tasks derived from the Knowledge Map / challenge system."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from backend.bench import repository as repo_mod
from backend.bench import suites
from backend.bench.models import AgentConfig, MAX_CASE_ATTEMPTS, MAX_RUN_ATTEMPTS
from backend.bench.repository import BenchConflict, BenchNotFound
from backend.bench.service import BenchService
from backend.knowledge import ids
from backend.tests.bench_fixtures import ScriptedLLM, by_model, llm_factory
from backend.tests.m7_fixtures import (  # noqa: F401  (pytest fixtures + helpers)
    NOW, S1, S3, FakeWorld, _fake_embeddings, cs, engine, evolving_scenario, make_user, repository, solar_claim, svc, world,
)

BASE = "preset-baseline"
FULL = "preset-full-corpus"
SMALL = "preset-small-model"
SUITE = "core-v1"


@pytest.fixture()
def bs(repository):
    return BenchService(repository, llm_factory=llm_factory("perfect"), clock=lambda: NOW)


@pytest.fixture()
def alice(repository):
    return make_user(repository, "Alice")


@pytest.fixture()
def bob(repository):
    return make_user(repository, "Bob")


def custom_task_payload(**over):
    d = {"title": "My own task", "question": "Does the custom sample pass the check?",
         "claims": [{"claim_id": "c1", "text": "The custom sample passes the check every time.", "expected_status": "verified", "supporting_docs": ["d1"]}],
         "corpus": [{"doc_id": "d1", "url": "https://mine.example.com/a", "title": "Mine", "content": "The custom sample passes the check every time it is run."}]}
    d.update(over)
    return d


def finished(bs, user, config=BASE, **kw):
    run = bs.create_run(user, config, suite_id=SUITE, **kw)
    view, ran = bs.execute_run(user, run["run_id"])
    assert ran and view["status"] == "completed", view
    return view


# ── overview / tasks / configs ───────────────────────────────────────────────
def test_overview_describes_everything_the_ui_needs(bs, alice):
    o = bs.overview(alice)
    assert o["suites"][0]["suite_id"] == SUITE and o["suites"][0]["task_count"] == 8 and len(o["suites"][0]["suite_hash"]) == 64
    assert {p["config_id"] for p in o["presets"]} == set(suites.PRESETS) and all(p["builtin"] for p in o["presets"])
    assert abs(sum(m["weight"] for m in o["metrics"]) - 1.0) < 1e-9 and {s["strategy"] for s in o["strategies"]} == {"full_corpus", "single_pass", "iterative"}
    assert any(m["model"] == "llama-3.3-70b-versatile" and m["price_in"] for m in o["models"]) and o["counts"] == {"custom_tasks": 0, "custom_configs": 0, "runs": 0}


def test_builtin_tasks_are_visible_to_everyone_and_custom_tasks_only_to_their_owner(bs, alice, bob):
    mine = bs.create_task(alice, custom_task_payload())["task_id"]
    assert mine.startswith("tsk_")
    a, b = bs.list_tasks(alice), bs.list_tasks(bob)
    assert a["total"] == 9 and b["total"] == 8 and mine in {t["task_id"] for t in a["items"]} and mine not in {t["task_id"] for t in b["items"]}
    assert [t["task_id"] for t in bs.list_tasks(alice, source="custom")["items"]] == [mine] and bs.list_tasks(alice, source="builtin")["total"] == 8
    assert {t["task_id"] for t in bs.list_tasks(bob, tag="injection")["items"]} == {"core-prompt-injection"}
    assert bs.get_task(bob, "core-contradiction")["definition"]["claims"][0]["expected_status"] == "conflicting"
    with pytest.raises(BenchNotFound):
        bs.get_task(bob, mine)
    with pytest.raises(BenchNotFound):
        bs.delete_task(bob, mine)


def test_custom_tasks_get_a_server_id_and_cannot_claim_derived_gold(bs, alice):
    t = bs.create_task(alice, {**custom_task_payload(), "task_id": "core-hijack", "gold_quality": "derived", "provenance": {"kind": "x"}})
    assert t["task_id"].startswith("tsk_") and t["gold_quality"] == "curated" and t["provenance"] is None and len(t["task_hash"]) == 64


def test_invalid_custom_tasks_are_rejected_before_anything_is_stored(bs, alice):
    bad = custom_task_payload()
    bad["claims"][0]["supporting_docs"] = ["nope"]
    with pytest.raises(ValidationError):
        bs.create_task(alice, bad)
    assert bs.list_tasks(alice, source="custom")["total"] == 0


def test_custom_task_cap_and_deletion(bs, alice, monkeypatch):
    monkeypatch.setattr(repo_mod, "MAX_CUSTOM_TASKS_PER_USER", 2)
    ids_ = [bs.create_task(alice, custom_task_payload(title=f"Task {i} here"))["task_id"] for i in range(2)]
    with pytest.raises(BenchConflict, match="at most 2"):
        bs.create_task(alice, custom_task_payload(title="One too many"))
    bs.delete_task(alice, ids_[0])
    bs.create_task(alice, custom_task_payload(title="Fits again"))
    with pytest.raises(BenchConflict, match="Built-in"):
        bs.delete_task(alice, "core-clean-support")


def test_configs_validate_the_model_and_are_scoped_to_their_owner(bs, alice, bob):
    c = bs.create_config(alice, {"name": "Mine", "model": "llama-3.1-8b-instant", "strategy": "iterative"})
    assert c["config_id"].startswith("cfg_") and c["price_known"] and not c["builtin"]
    with pytest.raises(ValueError, match="not allowed"):
        bs.create_config(alice, {"name": "Pricey", "model": "some-huge-expensive-model"})
    with pytest.raises(BenchConflict, match="already have"):
        bs.create_config(alice, {"name": "Mine", "model": "llama-3.1-8b-instant"})
    assert {x["config_id"] for x in bs.list_configs(alice)["items"]} >= {c["config_id"], *suites.PRESETS}
    assert c["config_id"] not in {x["config_id"] for x in bs.list_configs(bob)["items"]}
    with pytest.raises(BenchNotFound):
        bs.create_run(bob, c["config_id"], suite_id=SUITE)
    with pytest.raises(BenchConflict, match="Preset"):
        bs.delete_config(alice, BASE)
    bs.delete_config(alice, c["config_id"])
    with pytest.raises(BenchNotFound):
        bs.delete_config(alice, c["config_id"])


def test_the_application_default_model_is_allowed_but_unpriced(repository, alice):
    s = BenchService(repository, llm_factory=llm_factory(), clock=lambda: NOW, extra_models=("my-private-model",))
    c = s.create_config(alice, {"name": "Private", "model": "my-private-model"})
    assert c["model_known"] and not c["price_known"]
    view = finished(s, alice, c["config_id"])
    p = view["summary"]["performance"]
    assert p["total_tokens"] > 0 and p["cost_usd"] is None and p["cost_complete"] is False       # tokens known, cost honestly unknown


# ── creating runs ────────────────────────────────────────────────────────────
def test_run_requires_exactly_one_task_source_and_valid_references(bs, alice, bob):
    mine = bs.create_task(alice, custom_task_payload())["task_id"]
    for kw in ({}, {"task_ids": ["core-clean-support"], "suite_id": SUITE}):
        with pytest.raises(ValueError, match="exactly one"):
            bs.create_run(alice, BASE, **kw)
    with pytest.raises(BenchNotFound):
        bs.create_run(alice, BASE, suite_id="nope-v9")
    with pytest.raises(BenchNotFound):
        bs.create_run(alice, BASE, task_ids=["core-nonexistent"])
    with pytest.raises(BenchNotFound):
        bs.create_run(bob, BASE, task_ids=[mine])              # someone else's task
    with pytest.raises(BenchNotFound):
        bs.create_run(alice, "preset-nope", suite_id=SUITE)
    run = bs.create_run(alice, BASE, task_ids=["core-clean-support", mine, "core-clean-support"], label="  mixed  ")
    assert run["status"] == "pending" and run["case_count"] == 2 and run["label"] == "mixed" and run["summary"] is None


def test_run_task_count_is_bounded(bs, alice, monkeypatch):
    import backend.bench.service as s
    monkeypatch.setattr(s, "MAX_TASKS_PER_RUN", 3)
    with pytest.raises(ValueError, match="between 1 and 3"):
        bs.create_run(alice, BASE, suite_id=SUITE)


def test_run_cap_per_user_and_deletion_cascades(bs, alice, monkeypatch, repository):
    monkeypatch.setattr(repo_mod, "MAX_RUNS_PER_USER", 2)
    r1 = bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    with pytest.raises(BenchConflict, match="at most 2 runs"):
        bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    bs.delete_run(alice, r1["run_id"])
    assert repository.db.get_connection().execute("SELECT COUNT(*) FROM bench_run_cases WHERE run_id = ?", (r1["run_id"],)).fetchone()[0] == 0
    bs.create_run(alice, BASE, task_ids=["core-clean-support"])


# ── execution ────────────────────────────────────────────────────────────────
def test_a_full_suite_run_persists_scores_trajectories_usage_and_a_summary(bs, alice):
    view = finished(bs, alice)
    s = view["summary"]
    assert view["cases_done"] == 8 and view["cases_failed"] == 0 and s["completion_rate"] == 1.0 and not view["summary_partial"]
    assert s["composite"]["mean"] > 0.95 and all(m["n"] >= 1 for m in s["metrics"].values()) and s["metrics"]["contradiction_handling"]["n"] >= 3
    p = s["performance"]
    assert p["total_tokens"] > 0 and p["cost_usd"] > 0 and p["cost_complete"] and p["latency_ms_total"] >= 0 and p["tool_calls"] >= 8 * 2 - 2
    run = bs.get_run(alice, view["run_id"])
    assert [c["task_id"] for c in run["cases"]] == [t.task_id for t in suites.load_suite(SUITE).tasks]
    case = bs.inspect_case(alice, view["run_id"], "core-contradiction")
    assert case["status"] == "completed" and case["scores"]["contradiction_handling"] == 1.0 and case["output"]["claims"]
    assert [c["tool"] for c in case["output"]["calls"]][0] == "search_web" and case["trace"] and case["model_outputs"]
    assert case["task"]["corpus"] and case["detail"]["citations"] and case["usage"]["total_tokens"] > 0 and case["latency"]["total_ms"] >= 0


def test_execution_is_idempotent_and_never_pays_for_a_finished_run_twice(bs, alice):
    llm = ScriptedLLM("perfect")
    s = BenchService(bs.repository, llm_factory=lambda c: llm, clock=lambda: NOW)
    run = s.create_run(alice, BASE, suite_id=SUITE)
    first, ran1 = s.execute_run(alice, run["run_id"])
    calls = llm.calls
    again, ran2 = s.execute_run(alice, run["run_id"])
    assert ran1 and not ran2 and llm.calls == calls and again["summary"] == first["summary"] and again["attempt_count"] == 1


def test_fingerprint_identifies_what_was_measured(bs, alice):
    a, b = bs.create_run(alice, BASE, suite_id=SUITE), bs.create_run(alice, BASE, suite_id=SUITE)
    other = bs.create_run(alice, FULL, suite_id=SUITE)
    partial = bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    assert a["fingerprint"] == b["fingerprint"] and a["fingerprint"] not in {other["fingerprint"], partial["fingerprint"]}
    assert a["suite_hash"] == suites.load_suite(SUITE).suite_hash and partial["suite_hash"] is None


def test_reproduction_reruns_the_same_snapshot_and_gets_the_same_scores(bs, alice):
    original = finished(bs, alice)
    repro = bs.reproduce_run(alice, original["run_id"])
    assert repro["run_id"] != original["run_id"] and repro["reproduces_run_id"] == original["run_id"] and repro["fingerprint"] == original["fingerprint"]
    done, _ = bs.execute_run(alice, repro["run_id"])
    a, b = bs.get_run(alice, original["run_id"]), bs.get_run(alice, done["run_id"])
    assert [(c["task_id"], c["scores"], c["composite"]) for c in a["cases"]] == [(c["task_id"], c["scores"], c["composite"]) for c in b["cases"]]
    assert a["summary"]["metrics"] == b["summary"]["metrics"] and a["summary"]["composite"] == b["summary"]["composite"]


def test_a_run_is_a_snapshot_deleting_the_task_later_changes_nothing(bs, alice):
    tid = bs.create_task(alice, custom_task_payload())["task_id"]
    run = bs.create_run(alice, BASE, task_ids=[tid])
    bs.delete_task(alice, tid)
    done, _ = bs.execute_run(alice, run["run_id"])
    assert done["status"] == "completed" and bs.inspect_case(alice, run["run_id"], tid)["task"]["title"] == "My own task"
    assert bs.reproduce_run(alice, run["run_id"])["case_count"] == 1


def test_scoring_uses_the_snapshot_not_a_mutated_task_row(bs, alice, repository):
    tid = bs.create_task(alice, custom_task_payload())["task_id"]
    run = bs.create_run(alice, BASE, task_ids=[tid])
    conn = repository.db.get_connection()
    conn.execute("UPDATE bench_tasks SET task_json = '{\"garbage\": true}' WHERE task_id = ?", (tid,))
    conn.commit()
    assert bs.execute_run(alice, run["run_id"])[0]["status"] == "completed"


def test_cases_that_fail_are_retried_and_finished_cases_are_never_rerun(repository, alice):
    llm = ScriptedLLM("perfect", fail_times=2)          # the first two verification calls blow up
    s = BenchService(repository, llm_factory=lambda c: llm, clock=lambda: NOW)
    run = s.create_run(alice, BASE, suite_id=SUITE)
    first, _ = s.execute_run(alice, run["run_id"])
    assert first["status"] == "failed" and first["error_type"] == "cases_failed" and first["cases_failed"] == 2 and first["cases_done"] == 5 + 1   # 1 abstains w/o calling the model
    assert first["retryable"] and first["summary_partial"] and first["summary"]["cases_completed"] == 6
    calls = llm.calls
    second, ran = s.execute_run(alice, run["run_id"])
    assert ran and second["status"] == "completed" and second["cases_done"] == 8 and second["attempt_count"] == 2 and not second["summary_partial"]
    assert llm.calls == calls + 2                        # exactly the two failed cases were re-run


def test_cases_that_keep_failing_end_the_run_and_stop_retrying(repository, alice):
    llm = ScriptedLLM("perfect", fail_times=10_000)
    s = BenchService(repository, llm_factory=lambda c: llm, clock=lambda: NOW)
    run = s.create_run(alice, BASE, task_ids=["core-clean-support", "core-contradiction"])
    for _ in range(MAX_CASE_ATTEMPTS):
        view, _ = s.execute_run(alice, run["run_id"])
    assert view["status"] == "failed" and view["error_type"] == "all_cases_failed" and view["cases_failed"] == 2 and not view["retryable"] and view["summary"] is None
    calls = llm.calls
    s.execute_run(alice, run["run_id"])
    assert llm.calls == calls                            # nothing left to retry, nothing spent


def test_an_unavailable_model_fails_the_run_cleanly_without_spending_anything(repository, alice):
    def broken(config):
        raise RuntimeError("no api key configured")

    s = BenchService(repository, llm_factory=broken, clock=lambda: NOW)
    run = s.create_run(alice, BASE, suite_id=SUITE)
    view, _ = s.execute_run(alice, run["run_id"])
    assert view["status"] == "failed" and view["error_type"] == "model_unavailable" and view["cases_done"] == 0 and view["retryable"]
    s._llm_factory = llm_factory("perfect")
    again, _ = s.execute_run(alice, run["run_id"])
    assert again["status"] == "completed" and again["attempt_count"] == 2


def test_runs_stop_being_retryable_after_the_attempt_limit(repository, alice):
    def broken(config):
        raise RuntimeError("down")

    s = BenchService(repository, llm_factory=broken, clock=lambda: NOW)
    run = s.create_run(alice, BASE, suite_id=SUITE)
    for _ in range(MAX_RUN_ATTEMPTS):
        s.execute_run(alice, run["run_id"])
    with pytest.raises(BenchConflict, match="failed 3 times"):
        s.execute_run(alice, run["run_id"])
    assert s.get_run(alice, run["run_id"])["retryable"] is False


def test_a_running_run_is_leased_and_an_abandoned_one_can_be_taken_over(repository, alice):
    s = BenchService(repository, llm_factory=llm_factory("perfect"), clock=lambda: NOW)
    run = s.create_run(alice, BASE, task_ids=["core-clean-support"])
    row, attempt = s.repo.begin_run(alice, run["run_id"], NOW)            # a worker starts and then dies
    with pytest.raises(BenchConflict, match="already executing"):
        s.execute_run(alice, run["run_id"])
    later = BenchService(repository, llm_factory=llm_factory("perfect"), clock=lambda: NOW + timedelta(hours=2))
    done, ran = later.execute_run(alice, run["run_id"])
    assert ran and done["status"] == "completed" and done["attempt_count"] == 2
    with pytest.raises(BenchConflict, match="superseded"):                # the dead worker waking up cannot write
        s.repo.record_case(alice, run["run_id"], "core-clean-support", attempt, status="completed", composite=1.0, scores={}, detail={}, output={},
                           trace=[], raw_llm=[], usage={}, latency={}, error_type=None, started_at=NOW, now=NOW)


def test_a_running_run_cannot_be_deleted(bs, alice):
    run = bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    bs.repo.begin_run(alice, run["run_id"], NOW)
    with pytest.raises(BenchConflict, match="running"):
        bs.delete_run(alice, run["run_id"])


# ── isolation ────────────────────────────────────────────────────────────────
def test_users_cannot_see_or_touch_each_others_runs(bs, alice, bob):
    run = finished(bs, alice)
    rid = run["run_id"]
    for call in (lambda: bs.get_run(bob, rid), lambda: bs.execute_run(bob, rid), lambda: bs.reproduce_run(bob, rid), lambda: bs.delete_run(bob, rid),
                 lambda: bs.inspect_case(bob, rid, "core-clean-support"), lambda: bs.compare(bob, [rid, rid + "x"])):
        with pytest.raises((BenchNotFound, ValueError)):
            call()
    assert bs.list_runs(bob)["total"] == 0 and bs.list_runs(alice)["total"] == 1
    mine = finished(bs, bob)
    with pytest.raises(BenchNotFound):
        bs.compare(bob, [mine["run_id"], rid])
    assert bs.repo.integrity_problems(alice) == [] and bs.repo.integrity_problems() == []


def test_list_runs_filters_and_paginates(bs, alice):
    a = finished(bs, alice, BASE)
    b = bs.create_run(alice, FULL, suite_id=SUITE)
    assert bs.list_runs(alice, status="pending")["items"][0]["run_id"] == b["run_id"]
    assert [r["run_id"] for r in bs.list_runs(alice, fingerprint=a["fingerprint"])["items"]] == [a["run_id"]]
    assert [r["run_id"] for r in bs.list_runs(alice, config_hash=b["config_hash"])["items"]] == [b["run_id"]]
    page = bs.list_runs(alice, limit=1, offset=1)
    assert page["total"] == 2 and len(page["items"]) == 1 and page["items"][0]["run_id"] == a["run_id"]


# ── comparison ───────────────────────────────────────────────────────────────
def test_comparing_models_on_identical_evidence_ranks_them_and_shows_the_cost_of_being_wrong(repository, alice):
    s = BenchService(repository, llm_factory=by_model({"llama-3.3-70b-versatile": "perfect", "llama-3.1-8b-instant": "fabricating"}), clock=lambda: NOW)
    big, small = finished(s, alice, BASE), finished(s, alice, SMALL)
    r = s.compare(alice, [big["run_id"], small["run_id"]])
    assert r["comparable"] and len(r["common_tasks"]) == 8 and r["warnings"] == [] and not r["identical_setup"]
    assert r["composite"]["best"] == big["run_id"] and r["wins"][big["run_id"]] >= 6
    by = {m["metric"]: m for m in r["metrics"]}
    for metric in ("claim_correctness", "evidence_grounding", "citation_accuracy"):
        assert by[metric]["best"] == big["run_id"] and by[metric]["values"][big["run_id"]] > by[metric]["values"][small["run_id"]]
    perf = {p["metric"]: p for p in r["performance"]}
    assert perf["total_tokens"]["values"][big["run_id"]] > 0 and perf["cost_usd"]["best"] in (big["run_id"], small["run_id"])


def test_strategy_comparison_on_the_same_model_is_a_clean_ablation(bs, alice):
    full, single = finished(bs, alice, FULL), finished(bs, alice, BASE)
    r = bs.compare(alice, [full["run_id"], single["run_id"]])
    assert r["comparable"] and r["runs"][0]["strategy"] == "full_corpus" and r["runs"][1]["strategy"] == "single_pass"
    assert next(m for m in r["metrics"] if m["metric"] == "trajectory_quality")["values"][single["run_id"]] is not None


def test_comparison_excludes_tasks_that_were_not_measured_identically(bs, alice):
    a = finished(bs, alice)
    tid = bs.create_task(alice, custom_task_payload())["task_id"]
    b = bs.create_run(alice, BASE, task_ids=["core-clean-support", "core-contradiction", tid])
    bs.execute_run(alice, b["run_id"])
    r = bs.compare(alice, [a["run_id"], b["run_id"]])
    assert set(r["common_tasks"]) == {"core-clean-support", "core-contradiction"} and r["identical_setup"] is False
    assert tid in r["excluded_tasks"] and "excluded" in r["warnings"][0] and r["comparable"]


def test_comparison_input_rules(bs, alice):
    done = finished(bs, alice)
    pending = bs.create_run(alice, BASE, suite_id=SUITE)
    with pytest.raises(ValueError, match="between 2 and 4"):
        bs.compare(alice, [done["run_id"]])
    with pytest.raises(ValueError, match="between 2 and 4"):
        bs.compare(alice, [done["run_id"], done["run_id"]])             # duplicates collapse to one run
    with pytest.raises(BenchConflict, match="not completed"):
        bs.compare(alice, [done["run_id"], pending["run_id"]])
    with pytest.raises(ValueError, match="between 2 and 4"):
        bs.compare(alice, [f"run_{i:032x}" for i in range(5)])


# ── integrity ────────────────────────────────────────────────────────────────
def test_integrity_check_flags_corrupted_state(bs, alice, repository):
    finished(bs, alice)
    assert bs.repo.integrity_problems() == []
    conn = repository.db.get_connection()
    conn.execute("UPDATE bench_run_cases SET scores_json = NULL WHERE task_id = 'core-clean-support'")
    conn.commit()
    assert any("completed case without scores" in p for p in bs.repo.integrity_problems())


def test_schema_rejects_a_completed_run_without_a_summary(bs, alice, repository):
    run = bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    conn = repository.db.get_connection()
    with pytest.raises(Exception, match="CHECK"):
        conn.execute("UPDATE bench_runs SET status = 'completed' WHERE run_id = ?", (run["run_id"],))


# ── Knowledge Map / challenge integration ────────────────────────────────────
NEW_A = "https://news.example.io/a"


def test_a_knowledge_map_claim_becomes_a_frozen_derived_task(bs, svc, alice, repository):
    evolving_scenario(repository, alice)
    cid, inv = solar_claim(svc, alice)
    task, created = bs.create_task_from_claim(alice, inv, cid, include_challenges=False)
    d = task["definition"]
    assert created and task["gold_quality"] == "derived" and task["builtin"] is False and "from-claim" in task["tags"]
    assert d["claims"][0]["text"] == "Solar capacity grew in Europe" and d["claims"][0]["expected_status"] == "verified"
    assert {doc["url"] for doc in d["corpus"]} == {S1, S3} and set(d["claims"][0]["supporting_docs"]) == {"d1", "d2"}
    assert all("Solar capacity grew in Europe" in doc["content"] for doc in d["corpus"])
    assert d["provenance"] == {"kind": "knowledge_claim", "claim_id": cid, "investigation_id": inv, "challenge_ids": [], "gold_rule": "derived-v1"}
    again, created2 = bs.create_task_from_claim(alice, inv, cid, include_challenges=False)
    assert not created2 and again["task_id"] == task["task_id"] and bs.list_tasks(alice, source="custom")["total"] == 1     # idempotent


def test_a_derived_task_runs_like_any_other(bs, svc, alice, repository):
    evolving_scenario(repository, alice)
    cid, inv = solar_claim(svc, alice)
    task, _ = bs.create_task_from_claim(alice, inv, cid, include_challenges=False)
    run = bs.create_run(alice, FULL, task_ids=[task["task_id"]])
    done, _ = bs.execute_run(alice, run["run_id"])
    assert done["status"] == "completed"   # the scripted model is built from builtin gold only, so just prove the pipeline accepts it


def test_completed_challenges_contribute_their_evidence_and_flip_the_derived_gold(bs, svc, cs, world, alice, repository):
    r = evolving_scenario(repository, alice)
    cid, inv = ids.claim_id(alice, "Solar capacity grew in Europe"), r["r2"]
    world.add(NEW_A, "contradict", 0.9, content="Independent audit: solar capacity in Europe did not grow, it fell.", published="2026-09-20", title="Audit")
    created, _ = cs.create(alice, inv, cid, "counter_evidence")
    cs.execute(alice, created["challenge_id"])
    plain, _ = bs.create_task_from_claim(alice, inv, cid, include_challenges=False)
    task, made = bs.create_task_from_claim(alice, inv, cid, include_challenges=True)
    d = task["definition"]
    assert made and task["task_id"] != plain["task_id"] and "challenged" in task["tags"]
    assert NEW_A in {doc["url"] for doc in d["corpus"]} and d["provenance"]["challenge_ids"] == [created["challenge_id"]]
    claim = d["claims"][0]
    assert claim["expected_status"] == "conflicting" and claim["contradicting_docs"] and claim["supporting_docs"]      # both sides, from real provenance
    audit = next(doc for doc in d["corpus"] if doc["url"] == NEW_A)
    assert audit["content"] == f"Evidence from {NEW_A}" and audit["doc_id"] in claim["contradicting_docs"]      # the persisted passage, not a refetch


def test_challenges_of_other_claims_or_unfinished_ones_are_not_mixed_in(bs, svc, cs, world, alice, repository):
    r = evolving_scenario(repository, alice)
    cid, inv = ids.claim_id(alice, "Solar capacity grew in Europe"), r["r2"]
    cs.create(alice, inv, cid, "counter_evidence")                                    # pending: never executed
    world.add(NEW_A, "contradict", 0.9, content="Independent audit: solar capacity in Europe did not grow.", published="2026-09-20")
    other_cid = ids.claim_id(alice, "Hydro is stable")
    oc, _ = cs.create(alice, inv, other_cid, "counter_evidence")
    cs.execute(alice, oc["challenge_id"])
    task, _ = bs.create_task_from_claim(alice, inv, cid, include_challenges=True)
    assert task["definition"]["provenance"]["challenge_ids"] == [] and NEW_A not in {d["url"] for d in task["definition"]["corpus"]}


def test_claim_to_task_is_ownership_checked_and_validates_the_claim(bs, svc, alice, bob, repository):
    r = evolving_scenario(repository, alice)
    cid, inv = solar_claim(svc, alice)
    with pytest.raises(BenchNotFound):
        bs.create_task_from_claim(bob, inv, cid)
    with pytest.raises(BenchNotFound):
        bs.create_task_from_claim(alice, inv, ids.claim_id(alice, "A claim nobody ever made"))
    with pytest.raises(BenchNotFound):
        bs.create_task_from_claim(alice, "rpt_" + "0" * 32, cid)


def test_a_weak_contradiction_does_not_become_gold(bs):
    items = [{"url": "https://s1.example.com/a", "title": "A", "date": "2026-01-01", "passage": "Reports say the thing happened as described.", "stance": "support", "strength": 0.8},
             {"url": "https://s2.example.com/b", "title": "B", "date": None, "passage": "A minor note questions one detail of the thing.", "stance": "contradict", "strength": 0.3}]
    t = BenchService._derive_task("The thing happened as described", "partially_verified", "inv", "clm", [], items)
    c = t.claims[0]
    assert c.expected_status == "partially_verified" and c.contradicting_docs == [] and len(t.corpus) == 2
    with pytest.raises(ValueError, match="no usable evidence"):
        BenchService._derive_task("The thing happened as described", "verified", "inv", "clm", [], [{**items[0], "passage": "short"}])


def test_exhausted_stale_running_run_is_terminalized_instead_of_stuck(bs, alice, repository):
    """A crash during the final attempt must not leave the run running forever."""
    from backend.bench.models import RUN_LEASE_SECONDS

    run = bs.create_run(alice, BASE, task_ids=["core-clean-support"])
    stale = (NOW - timedelta(seconds=RUN_LEASE_SECONDS + 5)).isoformat()
    with repository.db.get_connection() as conn:
        conn.execute(
            "UPDATE bench_runs SET status='running', attempt_count=?, updated_at=?, error_type=NULL WHERE run_id=? AND user_id=?",
            (MAX_RUN_ATTEMPTS, stale, run["run_id"], alice),
        )
        conn.commit()

    with pytest.raises(BenchConflict, match="failed"):
        bs.execute_run(alice, run["run_id"])

    persisted = bs.repo.get_run(alice, run["run_id"])
    assert persisted["status"] == "failed"
    assert persisted["error_type"] == "interrupted_max_attempts_exhausted"
    assert persisted["completed_at"] is not None
