"""Phase 1B — the tools wrap the REAL Atlas services correctly.

test_tool_registry.py proves behaviour with fakes; this module guards against
the fakes hiding drift. It checks (a) the real services' signatures still
accept what the handlers pass, and (b) each tool end-to-end against the real
classes, with only the network/LLM/embedding edges replaced (same approach as
the existing suites). Nothing here touches the network.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")

from backend.agentic.tools import ToolDependencies, build_default_registry
from backend.models.domain_models import SearchResult


# ── (a) signature contracts ──────────────────────────────────────────────────
def test_real_service_signatures_accept_what_the_handlers_pass():
    from backend.agents.verification_agent import VerificationAgent
    from backend.database.repositories import ReportRepository
    from backend.memory import VectorMemoryService
    from backend.services.retrieval_service import RetrievalService
    from backend.services.scraping_service import AsyncScrapingService

    inspect.signature(RetrievalService.retrieve_and_rank).bind(
        object(), query="q", top_k=5, research_mode="explore")
    inspect.signature(AsyncScrapingService.scrape_sources).bind(object(), [])
    inspect.signature(VectorMemoryService.search_similar).bind(
        object(), query="q", user_id="u", limit=5, topic=None)
    inspect.signature(VerificationAgent.verify).bind(
        object(), report_markdown="claim", scraped_research_text="evidence")
    inspect.signature(ReportRepository.get_report).bind(object(), report_id="r", user_id="u")
    assert inspect.iscoroutinefunction(AsyncScrapingService.scrape_sources)


# ── search_web / request_more_evidence on the real RetrievalService ──────────
class FakeTavily:
    def __init__(self, count: int):
        self.count = count

    def search(self, query, max_results=None, *, time_range=None):
        n = min(self.count, max_results or self.count)
        return [
            SearchResult(title=f"Source {i}", url=f"https://site{i}.example.org/post", snippet=f"evidence {query}",
                         domain=f"site{i}.example.org", rank_position=i)
            for i in range(1, n + 1)
        ]


def _real_retrieval_registry(count: int = 12):
    from backend.services.retrieval_service import RetrievalService

    service = RetrievalService(tavily_service=FakeTavily(count))  # real RankingService
    return build_default_registry(ToolDependencies(retrieval_service=lambda: service))


def test_search_web_with_real_retrieval_and_ranking():
    result = _real_retrieval_registry().execute(
        "search_web", {"query": "solid state battery", "max_results": 5, "research_mode": "literature_review"})
    assert result.success, result.error
    sources = result.output.sources
    assert len(sources) == 5
    assert len({str(s.url) for s in sources}) == 5
    assert all(s.source_id for s in sources) and len({s.source_id for s in sources}) == 5
    assert [s.rank_position for s in sources] == sorted(s.rank_position for s in sources)


def test_request_more_evidence_with_real_retrieval_excludes_known_urls():
    known = [f"https://site{i}.example.org/post" for i in range(1, 4)]
    result = _real_retrieval_registry(12).execute("request_more_evidence", {
        "question": "solid state battery", "gap_description": "independent cost data",
        "exclude_urls": known, "max_results": 4})
    assert result.success, result.error
    urls = {str(s.url) for s in result.output.new_sources}
    assert len(result.output.new_sources) == 4
    assert not urls & {u for u in known}


# ── scrape_source: the SSRF guard is really in the path ──────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/internal",
    "http://[::1]/",
    "http://localhost/",
])
def test_scrape_source_cannot_reach_internal_addresses(url):
    from backend.services.scraping_service import AsyncScrapingService

    scraper = AsyncScrapingService()
    reg = build_default_registry(ToolDependencies(scraping_service=lambda: scraper))
    result = reg.execute("scrape_source", {"url": url})
    assert result.success, result.error  # the tool ran...
    assert result.output.success is False  # ...and the fetch was refused
    assert "Blocked" in (result.output.error_message or "")
    assert not (result.output.scraped and result.output.scraped.content)


# ── verify_claim on the real VerificationAgent (fake LLM only) ───────────────
def test_verify_claim_with_real_verification_agent(monkeypatch):
    from backend.agents.verification_agent import VerificationAgent

    payload = {
        "overall_verification_score": 72, "verdict": "partially_verified",
        "claims": [{
            "claim_text": "Solid-state cells reach 400 Wh/kg.", "confidence": 0.7, "status": "partially_verified",
            "evidence": [{"url": "https://example.com/a", "stance": "support", "title": "A",
                          "evidence_text": "Reports 400 Wh/kg in lab cells.", "evidence_strength": 0.6}],
        }],
    }

    class FakeLLM:
        def invoke(self, messages):
            self.messages = messages
            return SimpleNamespace(content=json.dumps(payload))

    agent = object.__new__(VerificationAgent)
    agent.llm = FakeLLM()
    monkeypatch.setattr("backend.agents.verification_agent.get_settings",
                        lambda: SimpleNamespace(max_evidence_chars_per_llm_call=10_000))
    reg = build_default_registry(ToolDependencies(verification_agent=lambda: agent))

    result = reg.execute("verify_claim", {"claim": "Solid-state cells reach 400 Wh/kg.",
                                          "evidence_text": "Source Title: A\nContent: Reports 400 Wh/kg in lab cells."})
    assert result.success, result.error
    summary = result.output.verification
    assert summary.verdict == "partially_verified" and summary.overall_verification_score == 72
    assert summary.claims[0].status == "partially_verified"
    assert str(summary.claims[0].evidence[0].url) == "https://example.com/a"
    # The claim under test is what the agent was asked to verify.
    assert "Solid-state cells reach 400 Wh/kg." in agent.llm.messages[1].content


def test_verify_claim_with_real_agent_reports_unparseable_llm_output_as_failure(monkeypatch):
    from backend.agents.verification_agent import VerificationAgent

    agent = object.__new__(VerificationAgent)
    agent.llm = SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="sorry, I cannot do that"))
    monkeypatch.setattr("backend.agents.verification_agent.get_settings",
                        lambda: SimpleNamespace(max_evidence_chars_per_llm_call=10_000))
    reg = build_default_registry(ToolDependencies(verification_agent=lambda: agent))
    result = reg.execute("verify_claim", {"claim": "A sufficiently long claim.", "evidence_text": "e"})
    assert not result.success and result.error.code == "execution_failed"


# ── search_existing_evidence on real SQLite + Chroma: tenant isolation ───────
def _fake_vector(text: str, dim: int = 16) -> list[float]:
    return [b / 255.0 for b in hashlib.sha256(text.encode("utf-8")).digest()[:dim]]


@pytest.fixture()
def memory_env(tmp_path, monkeypatch):
    from backend.core import config as config_module
    from backend.database import ResearchRepository
    from backend.memory import VectorMemoryService
    from backend.memory.embedding_service import EmbeddingService

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "sqlite" / "atlas.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    config_module.get_settings.cache_clear()
    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, text: _fake_vector(text))
    monkeypatch.setattr(EmbeddingService, "embed_texts", lambda self, texts: [_fake_vector(t) for t in texts])
    repo = ResearchRepository()
    repo.initialize()
    memory = VectorMemoryService(repository=repo)
    yield repo, memory
    config_module.get_settings.cache_clear()


def _seed_report(repo, memory, user_id: str, topic: str, body: str):
    from backend.models.domain_models import ResearchReport, ResearchSession

    topic_id = repo.topics.get_or_create_topic(topic, user_id=user_id)
    session = ResearchSession(topic=topic, status="completed")
    repo.sessions.save_session(topic_id, session, user_id=user_id)
    report = ResearchReport(report_id=f"report_{uuid.uuid4().hex[:12]}", topic=topic,
                            summary=f"Summary: {body}", report_markdown=body, key_statistics=[], swot={})
    repo.reports.save_report(topic_id, session, report)
    memory.index_report(topic_id=topic_id, session=session, report=report, user_id=user_id)
    return report


def _make_user(repo, name: str) -> str:
    from backend.auth.security import hash_password

    pw, salt = hash_password("Sup3rSecret!")
    return repo.users.create(email=f"{name.lower()}@example.com", name=name, password_hash=pw, password_salt=salt)["user_id"]


def test_search_existing_evidence_finds_own_reports_and_never_anothers(memory_env):
    repo, memory = memory_env
    alice, bob = _make_user(repo, "Alice"), _make_user(repo, "Bob")
    alice_report = _seed_report(repo, memory, alice, "Fusion energy", "Alice's private fusion findings")
    bob_report = _seed_report(repo, memory, bob, "Fusion energy", "Bob's private fusion findings")
    reg = build_default_registry(ToolDependencies(vector_memory=lambda: memory))

    a = reg.execute("search_existing_evidence", {"query": "fusion energy findings", "user_id": alice})
    b = reg.execute("search_existing_evidence", {"query": "fusion energy findings", "user_id": bob})
    stranger = reg.execute("search_existing_evidence", {"query": "fusion energy findings", "user_id": "user_nobody"})

    assert a.success and b.success and stranger.success
    assert [h.report_id for h in a.output.hits] == [alice_report.report_id]
    assert [h.report_id for h in b.output.hits] == [bob_report.report_id]
    assert stranger.output.hits == []
    assert "Alice" in a.output.hits[0].summary and "Bob" not in a.output.hits[0].summary
    assert 0.0 <= a.output.hits[0].similarity_score <= 1.0


def test_search_existing_evidence_on_empty_memory_returns_no_hits(memory_env):
    _, memory = memory_env
    reg = build_default_registry(ToolDependencies(vector_memory=lambda: memory))
    result = reg.execute("search_existing_evidence", {"query": "anything at all", "user_id": "user_x"})
    assert result.success and result.output.hits == []
