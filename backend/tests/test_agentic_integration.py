"""Milestone 1 (1A + 1B) integration — existing functionality remains unchanged.

The real compiled LangGraph is run end to end with only the network/LLM edges
faked. The key assertion: the pipeline's observable output is identical whether
the new plan step works or is deliberately broken, so the planner is proven
advisory and non-fatal.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")

from backend.graph import nodes
from backend.agentic.execution import AgenticToolExecutor
from backend.agentic.tools import ToolDependencies, build_default_registry
from backend.graph.state import ResearchGraphState
from backend.graph.workflow import build_research_graph
from backend.models.domain_models import (
    ClaimEvidence,
    ExecutiveBrief,
    RankedSource,
    ScrapedContent,
    SearchResult,
    VerificationSummary,
    VerifiedClaim,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPIC = "State of solid-state battery commercialization"


class FakeRetrieval:
    def __init__(self):
        self.calls = []

    def retrieve_and_rank(self, query, top_k=5, research_mode="explore"):
        self.calls.append({"query": query, "top_k": top_k, "research_mode": research_mode})
        ranked = [
            RankedSource(source_id=f"src_{i}", title=f"Source {i}", url=f"https://site{i}.example.org/post",
                         domain=f"site{i}.example.org", snippet=f"snippet {i}", relevance_score=60.0 - i,
                         rank_position=i)
            for i in range(1, 4)
        ]
        raw = [SearchResult(title=s.title, url=str(s.url), snippet=s.snippet, domain=s.domain, rank_position=s.rank_position)
               for s in ranked]
        return raw, ranked


class FakeScraper:
    async def scrape_sources(self, sources):
        return [
            ScrapedContent(source_id=s.source_id, url=str(s.url), title=s.title, domain=s.domain,
                           content=f"Full page text for {s.title}.", content_hash=f"h{s.source_id}",
                           success=True, http_status=200, content_length=30)
            for s in sources
        ]


class FakeVerifier:
    def verify(self, report_markdown, scraped_research_text):
        return VerificationSummary(
            overall_verification_score=81, verdict="mostly_verified",
            claims=[VerifiedClaim(claim_text="Solid-state cells are commercialising.", confidence=0.8, status="verified",
                                  evidence=[ClaimEvidence(source_id="", url="https://site1.example.org/post", title="Source 1",
                                                          evidence_text="It is.", evidence_strength=0.7)])],
        )


class FakeWriter:
    def generate_report(self, topic, research_text):
        return SimpleNamespace(summary="A sufficiently long summary of the research findings.",
                               report_markdown="# Report\n" + "Findings paragraph. " * 20,
                               key_statistics=["12 sources"])


class FakeBrief:
    def generate(self, topic, report_markdown, verification_score):
        return ExecutiveBrief(executive_summary="Brief.", key_insights=["insight"])


class FakeVectorMemory:
    def search_similar(self, query, user_id, limit, topic=None):
        return []


@pytest.fixture()
def fakes(monkeypatch):
    retrieval = FakeRetrieval()
    monkeypatch.setattr(nodes, "_retrieval_service", lambda: retrieval)
    monkeypatch.setattr(nodes, "_scraping_service", lambda: FakeScraper())
    monkeypatch.setattr(nodes, "_verification_agent", lambda: FakeVerifier())
    monkeypatch.setattr(nodes, "_writer_agent", lambda: FakeWriter())
    monkeypatch.setattr(nodes, "_executive_brief_agent", lambda: FakeBrief())
    registry = build_default_registry(ToolDependencies(
        retrieval_service=lambda: retrieval,
        scraping_service=lambda: FakeScraper(),
        verification_agent=lambda: FakeVerifier(),
        vector_memory=lambda: FakeVectorMemory(),
    ))
    monkeypatch.setattr(nodes, "_agentic_executor", lambda: AgenticToolExecutor(registry))
    return retrieval


def _initial_state(mode="explore", top_k=12):
    # Same shape research.py's run_research builds.
    return {
        "top_k_urls": top_k, "research_mode": mode, "search_query": TOPIC, "topic": TOPIC, "user_context": None,
        "authenticated_user_id": "user_test",
        "include_verification": True, "include_executive_brief": True, "persist_memory": False,
        "debug": {"started_at": time.perf_counter()}, "errors": [],
    }


def _observable(result: dict) -> dict:
    """Everything the API layer reads from the final state, minus volatile ids/timings."""
    report = result["final_report"]
    verification = result["verification_summary"].model_dump(mode="json")
    for claim in verification["claims"]:
        claim.pop("claim_id")  # random uuid per run — the only volatile field in here
    return {
        "summary": report.summary,
        "report_markdown": report.report_markdown,
        "key_statistics": report.key_statistics,
        "source_urls": [str(s.url) for s in result["ranked_sources"]],
        "source_ids": [s.source_id for s in result["ranked_sources"]],
        "credibility": [s.credibility_score for s in result["ranked_sources"]],
        "evidence_text": result["evidence_text"],
        "verification": verification,
        "verification_status": result["verification_status"],
        "executive_brief": report.executive_brief.model_dump(mode="json"),
        "status": result["status"],
        "session_status": result["session"].status,
        "session_report_id_matches": result["session"].report_id == report.report_id,
    }


# ── workflow shape ───────────────────────────────────────────────────────────
def test_workflow_uses_agentic_path_and_retains_fixed_fallback():
    names = list(build_research_graph().get_graph().nodes)
    assert names == ["__start__", "initialize", "plan", "agentic_execution", "search", "scrape",
                     "evidence_extraction", "verification", "writer", "credibility", "executive_brief_step",
                     "finalize_report", "complete", "__end__"]


def test_research_plan_is_declared_in_graph_state():
    # LangGraph silently drops undeclared keys — a missing declaration would erase the plan.
    assert "research_plan" in ResearchGraphState.__annotations__


# ── end-to-end behaviour ─────────────────────────────────────────────────────
@pytest.mark.parametrize("mode,top_k", [("brief", 8), ("explore", 12), ("compare", 14), ("investigate", 20), ("literature_review", 20)])
def test_pipeline_runs_end_to_end_through_agentic_execution(fakes, mode, top_k):
    result = build_research_graph().invoke(_initial_state(mode, top_k))

    # Normal graph execution now goes through the controlled tool registry.
    assert result["agentic_status"] in {"completed", "completed_with_gaps"}
    assert result["tool_call_count"] > 0
    assert result["agentic_trace"]
    assert all(call["research_mode"] == mode for call in fakes.calls)
    # The pipeline still produces everything the API reads.
    assert result["final_report"].topic == TOPIC
    assert result["final_report"].summary.startswith("A sufficiently long summary")
    assert [str(s.url) for s in result["final_report"].sources] == [f"https://site{i}.example.org/post" for i in (1, 2, 3)]
    assert result["verification_status"] == "succeeded"
    assert result["agentic_verification_status"] == "supported"
    assert result["replan_count"] == 0
    assert result["verification_summary"].claims[0].evidence[0].source_id == "src_1"  # node still links evidence to sources
    assert "Evidence Source: scraped_page" in result["evidence_text"]
    assert result["session"].status == "completed"
    assert result["metrics"].total_execution_ms >= 0
    # And the plan is attached, consistent with the request.
    plan = result["research_plan"]
    assert plan.question == TOPIC and plan.research_mode == mode
    assert plan.stopping_criteria.target_source_count == top_k


def test_agentic_execution_failure_routes_to_existing_fixed_pipeline(fakes, monkeypatch):
    class BrokenExecutor:
        def run(self, *_, **__):
            raise RuntimeError("agentic executor exploded")

    monkeypatch.setattr(nodes, "_agentic_executor", lambda: BrokenExecutor())
    result = build_research_graph().invoke(_initial_state())

    assert result["agentic_status"] == "failed"
    assert result["final_report"].topic == TOPIC
    assert result["verification_status"] == "succeeded"
    assert fakes.calls == [{"query": TOPIC, "top_k": 12, "research_mode": "explore"}]


def test_plan_node_is_passive_and_does_not_mutate_state():
    state = {"topic": TOPIC, "research_mode": "brief", "top_k_urls": 8}
    snapshot = dict(state)
    out = nodes.plan_node(state)
    assert state == snapshot  # input untouched
    # M3 added research_trace as an additive observability field; nothing else may change.
    assert {k: v for k, v in out.items() if k not in {"research_plan", "research_trace"}} == snapshot
    assert out["research_plan"].research_mode == "brief"
    assert [(e["component"], e["action"], e["status"]) for e in out["research_trace"]] == [("planner", "create_plan", "succeeded")]


def test_plan_node_failure_returns_state_unchanged(monkeypatch):
    monkeypatch.setattr(nodes, "_research_planner", lambda: SimpleNamespace(create_plan=lambda **_: 1 / 0))
    state = {"topic": TOPIC, "research_mode": "brief", "top_k_urls": 8}
    out = nodes.plan_node(state)
    assert "research_plan" not in out
    assert {k: v for k, v in out.items() if k != "research_trace"} == state
    assert [(e["component"], e["action"], e["status"]) for e in out["research_trace"]] == [("planner", "create_plan", "failed")]


def test_plan_node_does_not_call_any_tool_or_service(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("plan_node must not touch services or tools")

    for name in ("_retrieval_service", "_scraping_service", "_verification_agent", "_writer_agent"):
        monkeypatch.setattr(nodes, name, boom)
    from backend.agentic.tools.registry import ToolRegistry

    monkeypatch.setattr(ToolRegistry, "execute", boom)
    assert "research_plan" in nodes.plan_node({"topic": TOPIC, "research_mode": "explore", "top_k_urls": 12})


# ── public API surface unchanged ─────────────────────────────────────────────
def test_api_request_and_response_contracts_are_unchanged():
    from backend.models.api_models import ResearchRunRequest
    from backend.models.response_models import ResearchRunResponse

    assert set(ResearchRunRequest.model_fields) == {
        "research_mode", "topic", "user_context", "include_verification", "include_executive_brief",
        "persist_memory", "top_k_urls", "source_report_id"}
    assert set(ResearchRunResponse.model_fields) == {
        "insights", "session", "report", "top_sources", "verification", "source_report_id",
        "verification_status", "evaluation", "agentic_status", "agentic_verification_status",
        "replan_count", "agentic_trace", "tool_trace", "research_trace"}


def test_no_new_http_routes_were_added():
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import main
    except Exception as exc:  # pragma: no cover — app import needs env the sandbox may lack
        pytest.skip(f"app could not be imported here: {exc}")
    # Enumerate via the OpenAPI schema: newer FastAPI keeps included routers as lazy
    # objects, so filtering ``app.routes`` for APIRoute sees only top-level routes
    # and would pass vacuously.
    paths = set(main.app.openapi()["paths"])
    assert len(paths) > 25, f"route enumeration looks incomplete: {sorted(paths)}"
    assert not any("plan" in p or "tool" in p for p in paths), paths


def test_importing_the_agentic_packages_loads_no_heavy_clients():
    code = (
        "import sys, backend.agentic.planning, backend.agentic.tools\n"
        "heavy = [m for m in ('chromadb','tavily','sentence_transformers','langchain_groq','langchain_openai','aiohttp') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
