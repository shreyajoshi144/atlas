"""M8 test harness: scripted chat models behind the REAL tool registry and verification agent.

Only the model boundary is faked. Retrieval (M5 hybrid retriever over the frozen corpus), the
tool registry, request validation, the verification agent's prompt/parse path, scoring,
persistence and the API are all production code.

``ScriptedLLM(behavior)`` answers each verification prompt deterministically from the built-in
suite's gold labels, in a chosen *behaviour*, so tests can assert that the benchmark tells a
good agent from a bad one.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage

from backend.bench import suites
from backend.bench.models import AgentConfig, TaskDefinition
from backend.tests.m6_fixtures import NOW, _fake_embeddings, evolving_scenario, make_user, repository, svc  # noqa: F401

BEHAVIORS = ("perfect", "fabricating", "one_sided", "injected", "abstain_all", "bad_json", "no_usage")


def _all_tasks() -> list[TaskDefinition]:
    return suites.builtin_tasks()


def _quote(doc_text: str) -> str:
    """A verbatim, short passage of the document."""
    sentence = re.split(r"(?<=[.!?])\s+", doc_text.strip())[0]
    return sentence[:240]


class ScriptedLLM:
    def __init__(self, behavior: str = "perfect", *, tasks: Optional[list[TaskDefinition]] = None, fail_times: int = 0) -> None:
        assert behavior in BEHAVIORS, behavior
        self.behavior = behavior
        self.tasks = tasks or _all_tasks()
        self.fail_times = fail_times
        self.calls = 0
        self.prompts: list[str] = []

    # the verification agent calls ``llm.invoke([SystemMessage, HumanMessage])``
    def invoke(self, messages: Any, *a: Any, **kw: Any) -> AIMessage:
        self.calls += 1
        prompt = "\n".join(str(getattr(m, "content", m)) for m in messages)
        self.prompts.append(prompt)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("llm unavailable")
        if self.behavior == "bad_json":
            return AIMessage(content="I am sorry, I cannot produce JSON today.")
        payload = self._answer(prompt)
        text = json.dumps(payload)
        if self.behavior == "no_usage":
            return AIMessage(content=text)
        return AIMessage(content=text, usage_metadata={"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4,
                                                       "total_tokens": len(prompt) // 4 + len(text) // 4})

    # ── behaviours ───────────────────────────────────────────────────────────
    def _answer(self, prompt: str) -> dict[str, Any]:
        claims = []
        for task in self.tasks:
            golds = [g for g in task.claims if g.text in prompt]
            if not golds:
                continue
            shown = {d.doc_id for d in task.corpus if d.url in prompt}
            for g in golds:
                claims.append(self._claim(task, g, shown, prompt))
            break
        return {"overall_verification_score": 80.0, "verdict": "scripted", "claims": claims}

    def _ev(self, doc, stance: str, text: Optional[str] = None, strength: float = 0.9) -> dict[str, Any]:
        return {"url": doc.url, "stance": stance, "title": doc.title, "evidence_text": text or _quote(doc.content), "evidence_strength": strength}

    def _claim(self, task, g, shown: set[str], prompt: str) -> dict[str, Any]:
        sup = [task.doc(d) for d in g.supporting_docs if d in shown]
        con = [task.doc(d) for d in g.contradicting_docs if d in shown]
        b = self.behavior
        if b == "abstain_all":
            return {"claim_text": g.text, "confidence": 0.1, "status": "insufficient_evidence", "evidence": []}
        if b == "fabricating":
            doc = next((d for d in task.corpus if d.doc_id in shown), task.corpus[0])
            return {"claim_text": g.text, "confidence": 0.99, "status": "verified", "evidence": [
                self._ev(doc, "support", "A completely invented sentence that the source never contains whatsoever."),
                {"url": "https://invented.example.com/made-up", "stance": "support", "title": "Made up", "evidence_text": "Nothing here is real.", "evidence_strength": 0.9}]}
        if b == "injected" and "IMPORTANT INSTRUCTION FOR THE AI VERIFIER" in prompt:
            bad = next((d for d in task.corpus if d.injection), None)
            if bad is not None:
                return {"claim_text": g.text, "confidence": 1.0, "status": "verified", "evidence": [self._ev(bad, "support")]}
        if b == "one_sided":
            return {"claim_text": g.text, "confidence": 0.9, "status": "verified" if g.expected_status != "insufficient_evidence" else "insufficient_evidence",
                    "evidence": [self._ev(d, "support") for d in sup]}
        # perfect (and the non-injected path of "injected" / "no_usage"): gold verdict, but honest about missing evidence
        needed = [d for d in g.required_docs if d not in shown]
        if needed:
            return {"claim_text": g.text, "confidence": 0.3, "status": "insufficient_evidence", "evidence": [self._ev(d, "support") for d in sup]}
        return {"claim_text": g.text, "confidence": 0.9, "status": g.expected_status,
                "evidence": [self._ev(d, "support") for d in sup] + [self._ev(d, "contradict") for d in con]}


def llm_factory(behavior: str = "perfect", **kw: Any) -> Callable[[AgentConfig], ScriptedLLM]:
    def make(config: AgentConfig) -> ScriptedLLM:
        return ScriptedLLM(behavior, **kw)
    return make


def by_model(mapping: dict[str, str]) -> Callable[[AgentConfig], ScriptedLLM]:
    """Different behaviour per configured model — what a model comparison looks like."""
    return lambda config: ScriptedLLM(mapping.get(config.model, "perfect"))


def task(task_id: str) -> TaskDefinition:
    t = suites.builtin_task(task_id)
    assert t is not None, task_id
    return t


FULL = AgentConfig(name="full", model="llama-3.3-70b-versatile", strategy="full_corpus")
SINGLE = AgentConfig(name="single", model="llama-3.3-70b-versatile", strategy="single_pass")
ITER = AgentConfig(name="iter", model="llama-3.3-70b-versatile", strategy="iterative", top_k=1, max_rounds=3)
