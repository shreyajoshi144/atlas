from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.verification_agent import VerificationAgent, VerificationOutput
from backend.graph import nodes
from backend.models.domain_models import VerificationSummary


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def invoke(self, messages):
        return self.response


def _payload():
    return {
        "overall_verification_score": 88,
        "verdict": "mostly_verified",
        "claims": [
            {
                "claim_text": "Nuclear construction projects can have high capital costs.",
                "confidence": 0.91,
                "status": "verified",
                "evidence": [
                    {
                        "url": "https://example.com/nuclear",
                        "stance": "support",
                        "title": "Nuclear costs",
                        "evidence_text": "The source reports high construction costs.",
                        "evidence_strength": 0.9,
                    }
                ],
            },
            {
                "claim_text": "Solar projects can have lower installation costs.",
                "confidence": 0.84,
                "status": "partially_verified",
                "evidence": [],
            },
        ],
    }


def test_valid_structured_response_preserves_claims(monkeypatch):
    agent = object.__new__(VerificationAgent)
    agent.llm = FakeLLM(SimpleNamespace(content=__import__("json").dumps(_payload())))
    monkeypatch.setattr("backend.agents.verification_agent.get_settings", lambda: SimpleNamespace(max_evidence_chars_per_llm_call=10000))

    result = agent.verify("report", "evidence")

    assert isinstance(result, VerificationSummary)
    assert len(result.claims) == 2
    assert result.claims[0].claim_text == _payload()["claims"][0]["claim_text"]
    # ClaimEvidence.url is a Pydantic v2 HttpUrl, which does not compare equal
    # to a plain str (HttpUrl(...) == "..." is False even for the same
    # address) — cast to str for the comparison, matching how the API layer
    # already serializes it (str(e.url) in api/routes/research.py).
    assert str(result.claims[0].evidence[0].url) == "https://example.com/nuclear"


def test_tool_call_arguments_are_extracted_and_parsed():
    response = SimpleNamespace(
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "VerificationOutput", "arguments": __import__("json").dumps(_payload())},
            }
        ],
    )

    parsed = VerificationAgent._parse_verification_output(response)

    assert isinstance(parsed, VerificationOutput)
    assert len(parsed.claims) == 2
    assert parsed.claims[1].claim_text == _payload()["claims"][1]["claim_text"]


def test_malformed_response_becomes_typed_unverified_state(monkeypatch):
    class FailingAgent:
        def verify(self, **kwargs):
            raise ValueError("malformed verification JSON")

    monkeypatch.setattr(nodes, "_verification_agent", lambda: FailingAgent())
    state = {"include_verification": True, "evidence_text": "evidence", "ranked_sources": []}

    result = nodes.verification_node(state)

    assert result["verification_status"] == "failed"
    assert isinstance(result["verification_summary"], VerificationSummary)
    assert result["verification_summary"].claims == []
    assert result["verification_summary"].verdict == "unverified"


def test_writer_handoff_uses_verdict_score_while_claims_remain_in_verification_state(monkeypatch):
    captured = {}

    class FakeWriter:
        def generate_report(self, *, topic, research_text):
            captured["research_text"] = research_text
            return SimpleNamespace(summary="summary", report_markdown="report", key_statistics=[])

    monkeypatch.setattr(nodes, "_writer_agent", lambda: FakeWriter())
    verification = VerificationSummary.model_validate({
        "overall_verification_score": 90,
        "verdict": "verified",
        "claims": [{
            "claim_text": "Nuclear construction can require high upfront capital costs.",
            "confidence": 0.93,
            "status": "verified",
            "evidence": [{
                "source_id": "src_nuclear_1",
                "stance": "support",
                "publish_date": "2026-09-16",
                "url": "https://example.com/nuclear-costs",
                "title": "Nuclear construction costs",
                "evidence_text": "The source reports high upfront construction costs.",
                "evidence_strength": 0.91,
            }],
        }],
    })
    metrics = SimpleNamespace(llm_latency_ms=0)
    state = {
        "topic": "topic",
        "research_mode": "explore",
        "evidence_text": "grounded evidence",
        "verification_summary": verification,
        "metrics": metrics,
    }

    result = nodes.writer_node(state)

    assert result["generated_report_markdown"] == "report"
    assert "Verification Verdict: verified" in captured["research_text"]
    assert "Overall Verification Score: 90" in captured["research_text"]
    assert verification.claims[0].claim_text not in captured["research_text"]

    final_state = nodes.finalize_report_node({**state, **result})
    assert final_state["final_report"].verification is verification
    assert final_state["final_report"].verification.claims[0].claim_text == (
        "Nuclear construction can require high upfront capital costs."
    )


def test_verification_uses_deeper_evidence_budget(monkeypatch):
    """Deeper research evidence is not truncated to the old 6k budget."""
    from backend.agents.verification_agent import VerificationAgent

    captured = {}

    class DummyLLM:
        def invoke(self, messages):
            captured["evidence_chars"] = len(messages[1].content)
            return type("Response", (), {"content": '{"overall_verification_score": 100, "verdict": "verified", "claims": []}'})()

    monkeypatch.setattr(
        "backend.agents.verification_agent.get_settings",
        lambda: type("Settings", (), {"max_evidence_chars_per_llm_call": 24000})(),
    )
    monkeypatch.setattr("backend.agents.verification_agent.invoke_with_retry", lambda llm, messages: llm.invoke(messages))
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.llm = DummyLLM()

    agent.verify(report_markdown="", scraped_research_text="x" * 18000)
    assert captured["evidence_chars"] > 6000


def test_empty_report_verification_prompt_uses_evidence_as_claim_source(monkeypatch):
    captured = {}

    class DummyLLM:
        def invoke(self, messages):
            captured["prompt"] = messages[1].content
            return type("Response", (), {"content": '{"overall_verification_score": 80, "verdict": "verified", "claims": []}'})()

    monkeypatch.setattr(
        "backend.agents.verification_agent.get_settings",
        lambda: type("Settings", (), {"max_evidence_chars_per_llm_call": 24000})(),
    )
    monkeypatch.setattr("backend.agents.verification_agent.invoke_with_retry", lambda llm, messages: llm.invoke(messages))
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.llm = DummyLLM()

    agent.verify(report_markdown="", scraped_research_text="Nuclear construction can require high capital costs.")

    assert "No report has been generated yet" in captured["prompt"]
    assert "derive the factual claims directly from the supplied evidence corpus" in captured["prompt"]
    assert "Nuclear construction can require high capital costs." in captured["prompt"]
