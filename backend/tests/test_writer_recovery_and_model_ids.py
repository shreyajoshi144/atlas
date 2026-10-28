from types import SimpleNamespace


def test_writer_recovers_once_from_truncated_structured_output(monkeypatch):
    import backend.agents.writer_agent as mod
    calls = []
    valid = {"summary": "A sufficiently detailed grounded summary for this research report.",
             "report_markdown": "# Introduction\nEvidence-led.\n## Key Findings\nFinding.\n## Risks\nUncertainty.\n## Opportunities\nPotential.\n## Conclusion\nSummary.\n## Sources\n[W1]",
             "key_statistics": ["One supported finding"]}
    def invoke(_llm, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return SimpleNamespace(content='{"summary":"cut off', response_metadata={"finish_reason": "length"})
        return valid
    monkeypatch.setattr(mod, "invoke_with_retry", invoke)
    agent = object.__new__(mod.WriterAgent)
    agent.llm = object()
    result = agent.generate_report("topic", "[W1] Source evidence supports a finding.")
    assert result.summary == valid["summary"]
    assert len(calls) == 2
    assert "RECOVERY REQUIREMENT" in calls[1][1].content
    assert "[W1]" in calls[1][1].content


def test_default_model_uses_current_groq_catalog_replacement(monkeypatch):
    from backend.core.config import Settings
    settings = Settings(groq_api_key="test", tavily_api_key="test")
    assert settings.openai_model == "openai/gpt-oss-120b"


# ── Output budget / truncation regressions (live failure 2026-10-25) ─────────
# The writer used all 8192 completion tokens (finish_reason "length"); its
# recovery call then resent the same prompt with the same budget and Groq
# answered HTTP 413. These tests drive the real ChatGroq -> groq SDK -> httpx
# stack (through the router passthrough) against a mock transport, so they
# assert what actually goes over the wire.
import json

import httpx
import pytest

EVIDENCE = ("Source Title: Grounded Study\nURL: https://example.org/grounded-study\n"
            "Content: The study reports a 12% improvement in the measured outcome.")
VALID_REPORT = {
    "summary": "A sufficiently detailed grounded summary for this research report.",
    "report_markdown": ("# Introduction\nEvidence-led.\n## Key Findings\n12% improvement.\n## Risks\nUncertainty.\n"
                        "## Opportunities\nPotential.\n## Conclusion\nSummary.\n## Sources\n"
                        "- Grounded Study — https://example.org/grounded-study"),
    "key_statistics": ["12% improvement in the measured outcome"],
}


def _completion(content, finish_reason="stop", completion_tokens=900):
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 5391, "completion_tokens": completion_tokens, "total_tokens": 5391 + completion_tokens},
    })


def _too_large():
    return httpx.Response(413, json={"error": {
        "message": "Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 13628",
        "type": "tokens", "code": "rate_limit_exceeded"}})


def _writer_against(responses):
    """WriterAgent wired to a real ChatGroq whose HTTP layer replays ``responses``."""
    from langchain_groq import ChatGroq
    from backend.agents.writer_agent import WriterAgent
    from backend.routing.router import ModelRouter, RoutedLLM

    requests = []

    def handler(request):
        requests.append({"body": json.loads(request.content), "timeout": request.extensions.get("timeout", {})})
        return responses[len(requests) - 1]

    chat = ChatGroq(api_key="test", model="openai/gpt-oss-120b", max_tokens=8192, timeout=20, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    agent = object.__new__(WriterAgent)
    agent.llm = RoutedLLM(ModelRouter(client_factory=lambda _model: chat, default_model="openai/gpt-oss-120b"))
    return agent, requests


def _user_prompt(request):
    return next(m["content"] for m in request["body"]["messages"] if m["role"] == "user")


def test_writer_requests_bounded_report_with_writer_budget_and_timeout():
    agent, requests = _writer_against([_completion(json.dumps(VALID_REPORT))])
    result = agent.generate_report("topic", EVIDENCE)
    assert result.report_markdown == VALID_REPORT["report_markdown"]
    assert len(requests) == 1
    assert requests[0]["body"]["max_tokens"] == 8192
    assert requests[0]["timeout"].get("read") == 60          # writer timeout, not the global 20s
    prompt = _user_prompt(requests[0])
    assert "at most 8000" in prompt and "at most 700" in prompt
    assert "https://example.org/grounded-study" in prompt


def test_length_truncated_writer_output_recovers_with_smaller_budget_and_same_evidence():
    agent, requests = _writer_against([
        _completion('{"summary": "A summary that was cut off mid-str', finish_reason="length", completion_tokens=8192),
        _completion(json.dumps(VALID_REPORT)),
    ])
    result = agent.generate_report("topic", EVIDENCE)
    assert result.summary == VALID_REPORT["summary"]
    assert "https://example.org/grounded-study" in result.report_markdown
    assert len(requests) == 2
    assert requests[1]["body"]["max_tokens"] == 4096        # strictly smaller than the failed request
    recovery_prompt = _user_prompt(requests[1])
    assert "RECOVERY REQUIREMENT" in recovery_prompt
    assert EVIDENCE.splitlines()[1] in recovery_prompt      # full evidence (citation URL) resent unchanged
    assert "12% improvement" in recovery_prompt


def test_parseable_output_marked_length_is_not_accepted():
    # finish_reason "length" means the provider stopped early; the payload is
    # treated as incomplete even if a JSON object can be extracted from it.
    agent, requests = _writer_against([
        _completion(json.dumps(VALID_REPORT), finish_reason="length", completion_tokens=8192),
        _completion(json.dumps(VALID_REPORT)),
    ])
    agent.generate_report("topic", EVIDENCE)
    assert len(requests) == 2


def test_recovery_rejected_with_413_reports_provider_error_without_retrying():
    agent, requests = _writer_against([
        _completion('{"summary": "cut', finish_reason="length", completion_tokens=8192),
        _too_large(),
    ])
    with pytest.raises(ValueError) as excinfo:
        agent.generate_report("topic", EVIDENCE)
    message = str(excinfo.value)
    assert "truncated by the output-token limit" in message and "HTTP 413" in message
    assert len(requests) == 2                                  # a 413 is not retried


def test_recovery_truncation_is_reported_from_the_recovery_response():
    # First response is malformed but complete ("stop"); the recovery is the
    # one that hits the limit. The error must reflect the recovery's own
    # finish_reason rather than the first call's.
    agent, requests = _writer_against([
        _completion("not json at all"),
        _completion('{"summary": "cut', finish_reason="length", completion_tokens=4096),
    ])
    with pytest.raises(ValueError, match="truncated by the selected model"):
        agent.generate_report("topic", EVIDENCE)
    assert len(requests) == 2


def test_invoke_with_retry_forwards_call_options_and_does_not_retry_non_rate_limits():
    from backend.core.retry import invoke_with_retry

    seen = []

    class Runnable:
        def invoke(self, messages, **kwargs):
            seen.append(kwargs)
            raise type("APIStatusError", (Exception,), {"status_code": 413})("rate_limit_exceeded: request too large")

    with pytest.raises(Exception):
        invoke_with_retry(Runnable(), ["m"], max_retries=2, base_delay_seconds=0,
                          invoke_kwargs={"max_tokens": 4096, "timeout": 60})
    assert seen == [{"max_tokens": 4096, "timeout": 60}]


def test_writer_budget_settings_defaults():
    from backend.core.config import Settings
    settings = Settings(groq_api_key="test", tavily_api_key="test")
    assert settings.writer_max_output_tokens == 8192
    assert settings.writer_recovery_max_output_tokens < settings.writer_max_output_tokens
    assert settings.writer_request_timeout_seconds > settings.request_timeout_seconds
