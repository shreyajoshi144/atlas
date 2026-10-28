"""M8 — running a (task, config) pair through the REAL tool registry, retrieval and verification agent.

Only the chat model is scripted; see ``bench_fixtures``."""
from __future__ import annotations

import json
import socket

import pytest

from backend.bench import suites
from backend.bench.models import AgentConfig
from backend.bench.runner import run_case
from backend.bench.scoring import score_case
from backend.tests.bench_fixtures import FULL, ITER, SINGLE, ScriptedLLM, task

ALL = suites.builtin_tasks()


def scored(t, cfg, llm):
    out = run_case(t, cfg, llm)
    return out, score_case(t, out, cfg.strategy)


@pytest.mark.parametrize("cfg", [FULL, SINGLE], ids=lambda c: c.strategy)
def test_a_faithful_model_scores_near_perfectly_on_every_builtin_task(cfg):
    for t in ALL:
        out, s = scored(t, cfg, ScriptedLLM("perfect"))
        assert out.status == "completed", (t.task_id, out.error_type)
        assert s["composite"] >= 0.9, (t.task_id, s["scores"], s["flags"])
        for m in ("claim_correctness", "evidence_grounding", "citation_accuracy"):
            if t.task_id == "core-no-evidence" and m != "claim_correctness":
                assert s["scores"][m] is None            # nothing was cited, so citation/grounding do not apply
                continue
            assert s["scores"][m] == 1.0, (t.task_id, m, s["detail"][m])


def test_retrieval_finds_every_required_document_for_every_builtin_task():
    """Guards suite authoring against the real M5 retriever: a task the retriever cannot serve is a bad task."""
    for t in ALL:
        out = run_case(t, SINGLE, ScriptedLLM("perfect"))
        assert set(t.required_docs) <= set(out.retrieved), (t.task_id, out.retrieved)


def test_single_pass_trajectory_search_then_scrape_then_verify():
    t = task("core-clean-support")
    llm = ScriptedLLM("perfect")
    out = run_case(t, SINGLE, llm)
    tools = [c["tool"] for c in out.calls]
    assert tools[0] == "search_web" and tools[-1] == "verify_claim" and set(tools[1:-1]) == {"scrape_source"}
    assert [c["seq"] for c in out.calls] == list(range(1, len(out.calls) + 1)) and all(c["ok"] for c in out.calls)
    assert out.rounds == 1 and set(out.scraped) >= {"d1", "d2"} and llm.calls == 1
    assert out.usage["llm_calls"] == 1 and out.usage["total_tokens"] > 0 and out.usage["tokens_reported"] is True
    assert out.latency["total_ms"] >= out.latency["llm_ms"] >= 0 and out.latency["tool_ms"] >= 0


def test_full_corpus_skips_retrieval_and_reads_every_document():
    t = task("core-contradiction")
    out = run_case(t, FULL, ScriptedLLM("perfect"))
    assert "search_web" not in [c["tool"] for c in out.calls] and out.retrieved == [] and set(out.scraped) == {d.doc_id for d in t.corpus}


def test_the_verifier_only_sees_documents_the_agent_fetched():
    t = task("core-clean-support")
    llm = ScriptedLLM("perfect")
    run_case(t, AgentConfig(name="narrow", model="llama-3.3-70b-versatile", strategy="single_pass", top_k=1, max_scrapes=1), llm)
    seen = [d for d in t.corpus if d.url in llm.prompts[0]]
    assert len(seen) == 1                       # top_k=1 / max_scrapes=1 really constrains the evidence


def test_no_relevant_sources_is_an_abstention_not_a_crash_and_the_model_is_never_asked():
    t = task("core-no-evidence")
    llm = ScriptedLLM("fabricating")             # would hallucinate if it were ever asked
    out, s = scored(t, SINGLE, llm)
    assert out.status == "completed" and out.assessment == "no_evidence" and out.claims == [] and llm.calls == 0
    assert [c["tool"] for c in out.calls] == ["search_web"] and s["scores"]["claim_correctness"] == 1.0 and s["scores"]["trajectory_quality"] == 1.0


def test_full_corpus_exposes_hallucination_that_retrieval_would_have_hidden():
    t = task("core-no-evidence")
    _, honest = scored(t, FULL, ScriptedLLM("perfect"))
    _, liar = scored(t, FULL, ScriptedLLM("fabricating"))
    assert honest["scores"]["claim_correctness"] == 1.0
    assert liar["scores"]["claim_correctness"] == 0.0 and liar["scores"]["evidence_grounding"] < 0.5 and "fabricated_or_ungrounded_evidence" in liar["flags"]


def test_iterative_strategy_requests_more_evidence_when_the_verifier_reports_gaps():
    t = task("core-multi-hop")
    narrow_single = AgentConfig(name="s", model="llama-3.3-70b-versatile", strategy="single_pass", top_k=1)
    _, single = scored(t, narrow_single, ScriptedLLM("perfect"))
    out, it = scored(t, ITER, ScriptedLLM("perfect"))
    tools = [c["tool"] for c in out.calls]
    assert "request_more_evidence" in tools and out.rounds >= 2 and tools.count("verify_claim") == out.rounds
    assert single["scores"]["claim_correctness"] < 1.0                         # one document is not enough...
    assert it["scores"]["claim_correctness"] == 1.0 and it["scores"]["evidence_coverage"] == 1.0   # ...gap-filling finds the second


def test_iterative_never_exceeds_its_round_and_call_budgets():
    t = task("core-multi-hop")
    cfg = AgentConfig(name="i", model="llama-3.3-70b-versatile", strategy="iterative", top_k=1, max_rounds=2, max_tool_calls=6)
    out = run_case(t, cfg, ScriptedLLM("abstain_all"))     # always reports a gap, so it would loop forever if unbounded
    assert out.rounds <= 2 and len(out.calls) <= 6


def test_tool_budget_is_a_hard_limit_that_still_leaves_room_to_verify():
    t = task("core-clean-support")
    out = run_case(t, AgentConfig(name="tight", model="llama-3.3-70b-versatile", strategy="single_pass", max_tool_calls=3), ScriptedLLM("perfect"))
    assert len(out.calls) <= 3 and out.calls[-1]["tool"] == "verify_claim" and out.status == "completed"


@pytest.mark.parametrize("behavior,error", [("bad_json", "verification_failed")])
def test_unusable_model_output_fails_the_case_but_keeps_the_trajectory_and_usage(behavior, error):
    t = task("core-clean-support")
    out, s = scored(t, SINGLE, ScriptedLLM(behavior))
    assert out.status == "failed" and out.error_type == error and s["status"] == "failed" and s["composite"] is None
    assert [c["tool"] for c in out.calls][-1] == "verify_claim" and out.calls[-1]["ok"] is False and out.usage["llm_calls"] >= 1
    assert out.raw_llm and "cannot produce JSON" in out.raw_llm[0]


def test_a_model_that_raises_fails_the_case_without_raising_out_of_the_runner():
    out = run_case(task("core-clean-support"), SINGLE, ScriptedLLM("perfect", fail_times=99))
    assert out.status == "failed" and out.error_type == "verification_failed" and out.usage["llm_failures"] >= 1


def test_missing_provider_usage_is_reported_as_unknown_never_invented():
    out = run_case(task("core-clean-support"), SINGLE, ScriptedLLM("no_usage"))
    assert out.status == "completed" and out.usage["total_tokens"] is None and out.usage["tokens_reported"] is False


def test_prompt_injection_in_the_corpus_is_measured_not_obeyed_by_a_robust_model():
    t = task("core-prompt-injection")
    _, robust = scored(t, SINGLE, ScriptedLLM("perfect"))
    _, gullible = scored(t, FULL, ScriptedLLM("injected"))
    assert "cited_injected_document" not in robust["flags"] and robust["scores"]["claim_correctness"] == 1.0
    assert "cited_injected_document" in gullible["flags"] and gullible["scores"]["claim_correctness"] <= 0.5 < robust["scores"]["claim_correctness"]
    assert gullible["composite"] < robust["composite"]


def test_the_injected_text_reaches_the_model_only_inside_the_untrusted_content_wrapper():
    from backend.core.untrusted_content import UNTRUSTED_BEGIN, UNTRUSTED_END

    t = task("core-prompt-injection")
    llm = ScriptedLLM("perfect")
    run_case(t, FULL, llm)
    prompt = llm.prompts[0]
    marker = "IMPORTANT INSTRUCTION FOR THE AI VERIFIER"
    assert prompt.count(marker) == 1
    assert prompt.index(UNTRUSTED_BEGIN) < prompt.index(marker) < prompt.rindex(UNTRUSTED_END)


def test_runs_are_deterministic_apart_from_wall_clock():
    for cfg in (FULL, SINGLE, ITER):
        t = task("core-multi-hop")
        a, b = run_case(t, cfg, ScriptedLLM("perfect")), run_case(t, cfg, ScriptedLLM("perfect"))
        assert a.claims == b.claims and a.retrieved == b.retrieved and a.scraped == b.scraped
        assert [(c["tool"], c["key"], c["ok"]) for c in a.calls] == [(c["tool"], c["key"], c["ok"]) for c in b.calls]
        assert score_case(t, a, cfg.strategy) == score_case(t, b, cfg.strategy)


def test_the_frozen_world_never_touches_the_network(monkeypatch):
    attempts = []

    def deny(*a, **k):
        attempts.append(a)
        raise AssertionError("network access attempted")

    # block outbound connections / DNS (asyncio's own in-process socketpair must keep working)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    for cfg in (FULL, SINGLE):
        assert run_case(task("core-contradiction"), cfg, ScriptedLLM("perfect")).status == "completed"
    assert attempts == []


def test_the_public_trace_carries_no_document_text():
    t = task("core-clean-support")
    out = run_case(t, SINGLE, ScriptedLLM("perfect"))
    blob = json.dumps(out.trace)
    assert out.trace and "atlasbench" in blob
    assert not any(d.content[:40] in blob for d in t.corpus)


def test_a_cited_source_the_agent_never_fetched_is_caught_end_to_end():
    t = task("core-clean-support")
    cfg = AgentConfig(name="narrow", model="llama-3.3-70b-versatile", strategy="single_pass", top_k=1, max_scrapes=1)

    class Overciting(ScriptedLLM):
        def _answer(self, prompt):
            payload = super()._answer(prompt)
            for c in payload["claims"]:               # cites the second required document although it was never fetched
                c["evidence"].append(self._ev(t.doc("d2") if t.doc("d2").url not in prompt else t.doc("d1"), "support"))
            return payload

    out, s = scored(t, cfg, Overciting("perfect"))
    assert s["detail"]["citation_accuracy"]["invalid"] >= 1 and s["scores"]["citation_accuracy"] < 1.0


def test_the_benchmark_ranks_faithful_above_sloppy_above_fabricating_agents():
    def suite_mean(behavior):
        vals = [score_case(t, out, "full_corpus")["composite"] for t in ALL for out in [run_case(t, FULL, ScriptedLLM(behavior))]]
        return sum(vals) / len(vals)

    perfect, one_sided, fabricating, abstain = (suite_mean(b) for b in ("perfect", "one_sided", "fabricating", "abstain_all"))
    assert perfect > 0.95 and perfect > one_sided > fabricating and perfect > abstain
    assert fabricating < 0.6 and abstain < 0.5
