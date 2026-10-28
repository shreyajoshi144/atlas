"""Milestone 1 final integration/E2E tests for the normal research path."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")

pytest.importorskip("langgraph")

from backend.agentic.execution import AgenticToolExecutor
from backend.agentic.tools import ToolDependencies, build_default_registry
from backend.agentic.tools.schemas import SearchWebOutput
from backend.graph import nodes
from backend.graph.workflow import build_research_graph
from backend.models.api_models import ResearchRunRequest
from backend.models.domain_models import (
    ExecutiveBrief,
    RankedSource,
    ScrapedContent,
    SearchResult,
    VerificationSummary,
    VerifiedClaim,
)
from backend.models.common import VerificationStatus
from backend.api.routes import research as research_route
from backend.evaluation.models import EvaluationSummary


class Retrieval:
    def __init__(self):
        self.calls = []
        self.s1 = RankedSource(source_id="s1", title="Primary", url="https://primary.example/a", domain="primary.example", snippet="primary")
        self.s2 = RankedSource(source_id="s2", title="Independent", url="https://independent.example/b", domain="independent.example", snippet="independent")

    def retrieve_and_rank(self, query, top_k=5, research_mode="explore"):
        self.calls.append((query, top_k, research_mode))
        if "additional independent" in query.lower() or "directly supporting" in query.lower():
            sources = [self.s2]
        else:
            sources = [self.s1]
        raw = [SearchResult(title=s.title, url=s.url, snippet=s.snippet, domain=s.domain, rank_position=1) for s in sources]
        return raw, sources


class Scraper:
    async def scrape_sources(self, sources):
        return [
            ScrapedContent(
                source_id=s.source_id,
                url=s.url,
                title=s.title,
                domain=s.domain,
                content=f"Evidence from {s.source_id} supporting the Atlas claim.",
                content_hash=f"hash-{s.source_id}",
                success=True,
                http_status=200,
                content_length=40,
            )
            for s in sources
        ]


class Verifier:
    def __init__(self):
        self.calls = 0

    def verify(self, report_markdown, scraped_research_text):
        self.calls += 1
        if self.calls == 1:
            status = VerificationStatus.INSUFFICIENT_EVIDENCE
            score = 30
            verdict = "insufficient_evidence"
        else:
            status = VerificationStatus.VERIFIED
            score = 95
            verdict = "verified"
        return VerificationSummary(
            overall_verification_score=score,
            verdict=verdict,
            claims=[VerifiedClaim(claim_text="Atlas requires evidence.", confidence=0.95, status=status)],
        )


class Memory:
    def search_similar(self, **_kwargs):
        return []


class Writer:
    def generate_report(self, topic, research_text):
        return SimpleNamespace(
            summary="Final researched answer.",
            report_markdown="# Final Report\n" + "Evidence-backed finding. " * 20,
            key_statistics=["2 sources"],
        )


class Brief:
    def generate(self, topic, report_markdown, verification_score):
        return ExecutiveBrief(executive_summary="Brief", key_insights=["Verified insight"])


class Credibility:
    def score_sources(self, sources):
        return sources


@pytest.fixture()
def agentic_graph(monkeypatch):
    retrieval = Retrieval()
    scraper = Scraper()
    verifier = Verifier()
    registry = build_default_registry(
        ToolDependencies(
            retrieval_service=lambda: retrieval,
            scraping_service=lambda: scraper,
            verification_agent=lambda: verifier,
            vector_memory=lambda: Memory(),
        )
    )
    monkeypatch.setattr(nodes, "_agentic_executor", lambda: AgenticToolExecutor(registry))
    monkeypatch.setattr(nodes, "_writer_agent", lambda: Writer())
    monkeypatch.setattr(nodes, "_executive_brief_agent", lambda: Brief())
    monkeypatch.setattr(nodes, "_credibility_service", lambda: Credibility())
    return retrieval, verifier


def initial_state(topic="Atlas integration E2E", top_k=1):
    return {
        "top_k_urls": top_k,
        "research_mode": "explore",
        "search_query": topic,
        "topic": topic,
        "user_context": None,
        "authenticated_user_id": "user_a",
        "include_verification": True,
        "include_executive_brief": True,
        "persist_memory": False,
        "debug": {"started_at": time.perf_counter()},
        "errors": [],
    }


def test_normal_research_executes_full_agentic_replan_writer_flow(agentic_graph):
    retrieval, verifier = agentic_graph
    result = build_research_graph().invoke(initial_state())

    assert result["research_plan"] is not None
    assert result["agentic_status"] == "completed"
    assert result["tool_call_count"] <= result["research_plan"].stopping_criteria.max_tool_calls
    assert result["replan_count"] == 1
    assert verifier.calls == 2
    assert result["verification_summary"].verdict == "verified"
    assert result["agentic_verification_status"] == "supported"
    assert {str(source.url) for source in result["ranked_sources"]} == {
        "https://primary.example/a",
        "https://independent.example/b",
    }
    assert result["evidence_text"]
    assert result["final_report"].summary == "Final researched answer."
    assert any(event["event"] == "replan_created" for event in result["agentic_trace"])
    assert any(event["event"] == "re_verification" for event in result["agentic_trace"])
    assert len(retrieval.calls) >= 2


def test_normal_research_success_has_zero_unnecessary_replans(monkeypatch, agentic_graph):
    retrieval = Retrieval()
    verifier = Verifier()
    # This verifier succeeds immediately; the planned target is one source.
    def supported(*_args, **_kwargs):
        verifier.calls += 1
        return VerificationSummary(
            overall_verification_score=95,
            verdict="verified",
            claims=[VerifiedClaim(claim_text="Atlas requires evidence.", confidence=0.95, status=VerificationStatus.VERIFIED)],
        )
    verifier.verify = supported
    registry = build_default_registry(ToolDependencies(
        retrieval_service=lambda: retrieval,
        scraping_service=lambda: Scraper(),
        verification_agent=lambda: verifier,
        vector_memory=lambda: Memory(),
    ))
    monkeypatch.setattr(nodes, "_agentic_executor", lambda: AgenticToolExecutor(registry))
    monkeypatch.setattr(nodes, "_writer_agent", lambda: Writer())
    monkeypatch.setattr(nodes, "_executive_brief_agent", lambda: Brief())
    monkeypatch.setattr(nodes, "_credibility_service", lambda: Credibility())

    result = build_research_graph().invoke(initial_state())
    assert result["replan_count"] == 0
    assert verifier.calls == 1
    assert result["agentic_verification_status"] == "supported"


def test_normal_research_route_runs_evaluation_and_persistence(monkeypatch, agentic_graph):
    class Topics:
        def get_or_create_topic(self, topic, user_id):
            assert user_id == "user_a"
            return "topic_a"

    class Sessions:
        def __init__(self): self.saved = None
        def save_session(self, topic_id, session, user_id):
            assert user_id == "user_a"
            self.saved = session

    class Reports:
        def is_owned_by(self, report_id, user_id): return False
        def save_report(self, topic_id, session, report):
            assert topic_id == "topic_a"
            assert session.session_id
            assert report.verification is not None

    class Sources:
        def replace_sources(self, report_id, session_id, sources):
            assert report_id and session_id and sources

    class Evaluations:
        def __init__(self): self.saved = False
        def save_evaluation(self, report_id, session_id, topic_id, summary):
            self.saved = True
            assert report_id and session_id and topic_id == "topic_a"

    class Analytics:
        def __init__(self): self.events = []
        def save_event(self, event):
            self.events.append(event)
            assert event.session_id
            assert event.payload["replan_count"] == 1

    sessions, evaluations, analytics = Sessions(), Evaluations(), Analytics()
    repo = SimpleNamespace(
        topics=Topics(), sessions=sessions, reports=Reports(), sources=Sources(),
        evaluations=evaluations, analytics=analytics,
    )
    monkeypatch.setattr(research_route, "_repository", lambda: repo)
    monkeypatch.setattr(research_route, "_vector_memory", lambda: SimpleNamespace(index_report=lambda **_: None))
    monkeypatch.setattr(
        research_route,
        "run_pipeline_evaluation",
        lambda **kwargs: EvaluationSummary(report_id=kwargs["report_id"], session_id=kwargs["session_id"], topic=kwargs["topic"], overall_score=0.9),
    )

    response = asyncio.run(research_route.run_research(
        ResearchRunRequest(topic="Atlas integration E2E", top_k_urls=1, persist_memory=True),
        {"user_id": "user_a"},
    ))
    data = response.data
    assert data is not None
    assert data.agentic_status == "completed"
    assert data.replan_count == 1
    assert data.evaluation is not None
    assert data.report.report_id
    assert sessions.saved is not None
    assert evaluations.saved is True
    assert len(analytics.events) == 1


def test_user_scope_is_forwarded_to_agentic_executor(monkeypatch):
    seen = {}

    class Executor:
        def run(self, plan, *, execution_context, initial_state):
            seen["user_id"] = execution_context["user_id"]
            return {
                "research_plan": plan,
                "agentic_status": "completed",
                "current_action_id": None,
                "completed_action_ids": [],
                "tool_results": {},
                "action_tool_results": {},
                "tool_trace": [],
                "tool_call_count": 0,
                "execution_metrics": {},
                "verification_status": "supported",
                "evidence_gaps": [],
                "replan_count": 0,
                "replan_history": [],
                "newly_discovered_sources": [],
                "agentic_trace": [],
            }

    monkeypatch.setattr(nodes, "_agentic_executor", lambda: Executor())
    result = nodes.agentic_execution_node({
        **initial_state(),
        "research_plan": nodes._research_planner().create_plan("Atlas integration E2E", "explore", 1),
        "metrics": __import__("backend.models.common", fromlist=["MetricBreakdown"]).MetricBreakdown(),
    })
    assert seen["user_id"] == "user_a"
    assert result["agentic_status"] == "completed"
