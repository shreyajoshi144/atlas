"""Phase 1B — controlled tool registry: every entry, I/O validation, containment."""
from __future__ import annotations

from datetime import date
from types import MappingProxyType, SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from backend.agentic.common import ToolName, compose_query
from backend.agentic.tools import (
    ExistingEvidenceHit,
    RequestMoreEvidenceOutput,
    ScrapeSourceOutput,
    SearchExistingEvidenceOutput,
    SearchWebInput,
    SearchWebOutput,
    ToolDependencies,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    VerifyClaimOutput,
    build_default_registry,
    get_tool_registry,
)
from backend.models.domain_models import (
    ClaimEvidence,
    RankedSource,
    ScrapedContent,
    SearchResult,
    VerificationSummary,
    VerifiedClaim,
)


# ── fakes standing in for the real (networked / LLM-backed) services ─────────
def _ranked(n: int, prefix: str = "https://example.com/a") -> list[RankedSource]:
    return [
        RankedSource(title=f"Source {i}", url=f"{prefix}{i}", domain="example.com",
                     snippet="evidence", relevance_score=50.0, rank_position=i)
        for i in range(1, n + 1)
    ]


class FakeRetrieval:
    def __init__(self, ranked=None):
        self.ranked = ranked if ranked is not None else _ranked(3)
        self.calls: list[dict] = []

    def retrieve_and_rank(self, query, top_k=5, research_mode="explore"):
        self.calls.append({"query": query, "top_k": top_k, "research_mode": research_mode})
        raw = [SearchResult(title="t", url="https://example.com/raw", snippet="s", domain="example.com", rank_position=1)]
        return raw * 4, list(self.ranked)[:top_k]


class FakeScraper:
    def __init__(self, result=None, empty=False):
        self.result, self.empty, self.calls = result, empty, []

    async def scrape_sources(self, sources):
        self.calls.append(list(sources))
        if self.empty:
            return []
        if self.result is not None:
            return [self.result(sources[0])]
        src = sources[0]
        return [ScrapedContent(source_id=src.source_id, url=str(src.url), title=src.title, domain=src.domain,
                               content="Page body text.", content_hash="abc", success=True,
                               http_status=200, content_length=15, publish_date=date(2026, 9, 1))]


class FakeMemory:
    def __init__(self, matches, rows):
        self.matches, self.rows, self.search_calls, self.report_calls = matches, rows, [], []
        outer = self
        self.repository = SimpleNamespace(reports=SimpleNamespace(get_report=outer._get_report))

    def search_similar(self, query, user_id, limit=5, topic=None):
        self.search_calls.append({"query": query, "user_id": user_id, "limit": limit, "topic": topic})
        return self.matches

    def _get_report(self, report_id, user_id=None):
        self.report_calls.append((report_id, user_id))
        return self.rows.get((report_id, user_id))


class FakeVerifier:
    def __init__(self, error: Exception | None = None):
        self.error, self.calls = error, []

    def verify(self, report_markdown, scraped_research_text):
        self.calls.append({"report_markdown": report_markdown, "scraped_research_text": scraped_research_text})
        if self.error:
            raise self.error
        return VerificationSummary(
            overall_verification_score=80, verdict="mostly_verified",
            claims=[VerifiedClaim(claim_text="Claim text here", confidence=0.9, status="verified",
                                  evidence=[ClaimEvidence(source_id="", url="https://example.com/a1", title="T",
                                                          evidence_text="e", evidence_strength=0.8)])],
        )


def _registry(retrieval=None, scraper=None, memory=None, verifier=None) -> ToolRegistry:
    return build_default_registry(ToolDependencies(
        retrieval_service=lambda: retrieval or FakeRetrieval(),
        scraping_service=lambda: scraper or FakeScraper(),
        vector_memory=lambda: memory or FakeMemory([], {}),
        verification_agent=lambda: verifier or FakeVerifier(),
    ))


# ── registry shape ───────────────────────────────────────────────────────────
def test_registry_exposes_exactly_the_five_required_tools():
    reg = _registry()
    assert set(reg.names()) == set(ToolName)
    assert {n.value for n in reg.names()} == {
        "search_web", "scrape_source", "search_existing_evidence", "verify_claim", "request_more_evidence",
    }


@pytest.mark.parametrize("tool", list(ToolName), ids=lambda t: t.value)
def test_each_entry_has_name_description_and_typed_schemas(tool):
    spec = _registry().get(tool)
    assert spec.name == tool
    assert len(spec.description) > 30
    assert issubclass(spec.input_model, BaseModel) and issubclass(spec.output_model, BaseModel)
    described = spec.describe()
    assert described["name"] == tool.value
    assert described["input_schema"]["additionalProperties"] is False  # unknown params are refused
    assert described["output_schema"]["properties"]


def test_describe_lists_all_tools():
    assert {d["name"] for d in _registry().describe()} == {t.value for t in ToolName}


def test_default_registry_is_lazy_and_reuses_existing_singletons(monkeypatch):
    # Building the registry must not touch any service...
    def explode():
        raise AssertionError("service resolved at build time")

    build_default_registry(ToolDependencies(retrieval_service=explode, scraping_service=explode,
                                            vector_memory=explode, verification_agent=explode))
    # ...and the default providers must hand back the graph's cached instances, not new ones.
    from backend.agentic.tools import builtin
    from backend.graph import nodes

    sentinel = object()
    for attr, provider in (("_retrieval_service", builtin._default_retrieval_service),
                           ("_scraping_service", builtin._default_scraping_service),
                           ("_verification_agent", builtin._default_verification_agent)):
        monkeypatch.setattr(nodes, attr, lambda s=sentinel: s)
        assert provider() is sentinel
    assert isinstance(get_tool_registry(), ToolRegistry)


# ── search_web ───────────────────────────────────────────────────────────────
def test_search_web_delegates_to_retrieval_service():
    retrieval = FakeRetrieval(_ranked(5))
    result = _registry(retrieval=retrieval).execute("search_web", {"query": "solid state batteries", "max_results": 3, "research_mode": "investigate"})
    assert result.success, result.error
    assert retrieval.calls == [{"query": "solid state batteries", "top_k": 3, "research_mode": "investigate"}]
    out = result.output
    assert isinstance(out, SearchWebOutput)
    assert out.raw_result_count == 4 and len(out.sources) == 3
    assert all(isinstance(s, RankedSource) for s in out.sources)


def test_search_web_defaults():
    retrieval = FakeRetrieval()
    _registry(retrieval=retrieval).execute("search_web", {"query": "some query"})
    assert retrieval.calls[0]["top_k"] == 10 and retrieval.calls[0]["research_mode"] == "explore"


# ── scrape_source ────────────────────────────────────────────────────────────
def test_scrape_source_delegates_to_scraping_service():
    scraper = FakeScraper()
    result = _registry(scraper=scraper).execute(
        "scrape_source", {"url": "https://www.Example.com/post", "title": "A post", "source_id": "src_fixed"})
    assert result.success, result.error
    (sources,) = scraper.calls
    assert len(sources) == 1
    assert sources[0].source_id == "src_fixed" and sources[0].title == "A post"
    assert sources[0].domain == "example.com"  # www. stripped, lowercased
    out = result.output
    assert isinstance(out, ScrapeSourceOutput)
    assert out.success and out.source_id == "src_fixed"
    assert out.scraped.content == "Page body text." and out.scraped.publish_date == date(2026, 9, 1)


def test_scrape_source_reports_scraper_failure_as_data_not_exception():
    def failed(src):
        return ScrapedContent(source_id=src.source_id, url=str(src.url), title=src.title, domain=src.domain,
                              content="", content_hash="", success=False, http_status=403, error_message="HTTP 403")

    result = _registry(scraper=FakeScraper(result=failed)).execute("scrape_source", {"url": "https://example.com/x"})
    assert result.success  # the tool ran; the page just could not be fetched
    assert result.output.success is False and result.output.error_message == "HTTP 403"


def test_scrape_source_handles_scraper_returning_nothing():
    result = _registry(scraper=FakeScraper(empty=True)).execute("scrape_source", {"url": "https://example.com/x"})
    assert result.success and result.output.success is False and result.output.scraped is None
    assert result.output.error_message


async def test_scrape_source_works_inside_a_running_event_loop():
    result = _registry().execute("scrape_source", {"url": "https://example.com/x"})
    assert result.success, result.error


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "not a url", "", "example.com/x"])
def test_scrape_source_rejects_non_http_urls_before_any_fetch(url):
    scraper = FakeScraper()
    result = _registry(scraper=scraper).execute("scrape_source", {"url": url})
    assert not result.success and result.error.code == ToolErrorCode.INVALID_INPUT
    assert scraper.calls == []


# ── search_existing_evidence ─────────────────────────────────────────────────
def test_search_existing_evidence_is_scoped_to_the_user():
    memory = FakeMemory(
        matches=[{"report_id": "r1", "topic": "T1", "similarity_score": 0.91},
                 {"report_id": "r2", "topic": "T2", "similarity_score": 0.80}],
        rows={("r1", "user_a"): {"topic": "Topic One", "summary": "Summary one"}},  # r2 is not user_a's
    )
    result = _registry(memory=memory).execute(
        "search_existing_evidence", {"query": "battery costs", "user_id": "user_a", "limit": 4, "topic": "T1"})
    assert result.success, result.error
    assert memory.search_calls == [{"query": "battery costs", "user_id": "user_a", "limit": 4, "topic": "T1"}]
    assert {call[1] for call in memory.report_calls} == {"user_a"}  # ownership re-checked with the same user
    out = result.output
    assert isinstance(out, SearchExistingEvidenceOutput)
    assert [h.report_id for h in out.hits] == ["r1"]  # r2 dropped: not owned
    assert out.hits[0] == ExistingEvidenceHit(report_id="r1", topic="Topic One", summary="Summary one", similarity_score=0.91)


def test_search_existing_evidence_requires_a_user_id():
    memory = FakeMemory([], {})
    for payload in ({"query": "battery costs"}, {"query": "battery costs", "user_id": ""}, {"query": "battery costs", "user_id": "   "}):
        result = _registry(memory=memory).execute("search_existing_evidence", payload)
        assert not result.success and result.error.code == ToolErrorCode.INVALID_INPUT
    assert memory.search_calls == []


# ── verify_claim ─────────────────────────────────────────────────────────────
def test_verify_claim_delegates_to_verification_agent():
    verifier = FakeVerifier()
    result = _registry(verifier=verifier).execute(
        "verify_claim", {"claim": "Solid-state cells reach 400 Wh/kg.", "evidence_text": "Source says 400 Wh/kg."})
    assert result.success, result.error
    assert verifier.calls == [{"report_markdown": "Solid-state cells reach 400 Wh/kg.", "scraped_research_text": "Source says 400 Wh/kg."}]
    out = result.output
    assert isinstance(out, VerifyClaimOutput)
    assert isinstance(out.verification, VerificationSummary)
    assert out.verification.verdict == "mostly_verified" and len(out.verification.claims) == 1


def test_verify_claim_agent_failure_is_contained_and_never_fabricated():
    verifier = FakeVerifier(error=ValueError("Verification response could not be parsed into VerificationOutput"))
    result = _registry(verifier=verifier).execute(
        "verify_claim", {"claim": "A sufficiently long claim.", "evidence_text": "some evidence"})
    assert not result.success and result.output is None
    assert result.error.code == ToolErrorCode.EXECUTION_FAILED
    assert "could not be parsed" in result.error.message


# ── request_more_evidence ────────────────────────────────────────────────────
def test_request_more_evidence_excludes_seen_urls_and_overfetches():
    retrieval = FakeRetrieval(_ranked(6))
    result = _registry(retrieval=retrieval).execute("request_more_evidence", {
        "question": "Solid-state battery costs", "gap_description": "independent cost data",
        "exclude_urls": ["https://example.com/a1", "https://EXAMPLE.com/a2/"], "max_results": 2,
        "research_mode": "investigate",
    })
    assert result.success, result.error
    assert retrieval.calls == [{"query": "Solid-state battery costs independent cost data", "top_k": 4, "research_mode": "investigate"}]
    out = result.output
    assert isinstance(out, RequestMoreEvidenceOutput)
    assert out.query == "Solid-state battery costs independent cost data"
    assert out.filtered_already_seen == 2
    assert [str(s.url) for s in out.new_sources] == ["https://example.com/a3", "https://example.com/a4"]


def test_request_more_evidence_never_returns_more_than_requested_or_an_excluded_url():
    retrieval = FakeRetrieval(_ranked(10))
    out = _registry(retrieval=retrieval).execute("request_more_evidence", {
        "question": "topic here", "gap_description": "a gap", "max_results": 3}).output
    assert len(out.new_sources) == 3 and out.filtered_already_seen == 0


def test_request_more_evidence_can_return_nothing_honestly():
    retrieval = FakeRetrieval(_ranked(2))
    out = _registry(retrieval=retrieval).execute("request_more_evidence", {
        "question": "topic here", "gap_description": "a gap",
        "exclude_urls": ["https://example.com/a1", "https://example.com/a2"]}).output
    assert out.new_sources == [] and out.filtered_already_seen == 2


def test_compose_query_respects_the_300_char_limit_and_keeps_the_gap():
    q = compose_query("word " * 80, "the important gap")
    assert len(q) <= 300 and q.endswith("the important gap")
    assert compose_query("base", "") == "base"


# ── 3. tool input / output validation ────────────────────────────────────────
VALID_PAYLOADS = {
    "search_web": {"query": "a valid query"},
    "scrape_source": {"url": "https://example.com/page"},
    "search_existing_evidence": {"query": "a valid query", "user_id": "user_1"},
    "verify_claim": {"claim": "A sufficiently long claim.", "evidence_text": "evidence"},
    "request_more_evidence": {"question": "a question", "gap_description": "a gap"},
}

INVALID_PAYLOADS = [
    ("search_web", {}), ("search_web", {"query": "ab"}), ("search_web", {"query": "   "}),
    ("search_web", {"query": "x" * 301}), ("search_web", {"query": "ok query", "max_results": 0}),
    ("search_web", {"query": "ok query", "max_results": 21}), ("search_web", {"query": "ok query", "max_results": "many"}),
    ("search_web", {"query": "ok query", "research_mode": "deep_dive"}),
    ("scrape_source", {}), ("scrape_source", {"url": "https://example.com", "title": "t" * 301}),
    ("scrape_source", {"url": "https://example.com", "source_id": ""}),
    ("search_existing_evidence", {"user_id": "u"}), ("search_existing_evidence", {"query": "ok query", "user_id": "u", "limit": 11}),
    ("search_existing_evidence", {"query": "ok query", "user_id": "u", "limit": 0}),
    ("search_existing_evidence", {"query": "ok query", "user_id": "u" * 121}),
    ("verify_claim", {"claim": "short", "evidence_text": "e"}), ("verify_claim", {"claim": "A sufficiently long claim."}),
    ("verify_claim", {"claim": "A sufficiently long claim.", "evidence_text": "  "}),
    ("verify_claim", {"claim": "c" * 1001, "evidence_text": "e"}),
    ("verify_claim", {"claim": "A sufficiently long claim.", "evidence_text": "e" * 200_001}),
    ("request_more_evidence", {"question": "a question"}), ("request_more_evidence", {"gap_description": "a gap"}),
    ("request_more_evidence", {"question": "a question", "gap_description": "a gap", "max_results": 11}),
    ("request_more_evidence", {"question": "a question", "gap_description": "a gap", "exclude_urls": ["not-a-url"]}),
    ("request_more_evidence", {"question": "a question", "gap_description": "a gap", "exclude_urls": ["https://e.com/x"] * 101}),
]


@pytest.mark.parametrize("tool,payload", list(VALID_PAYLOADS.items()), ids=list(VALID_PAYLOADS))
def test_minimal_valid_payload_is_accepted(tool, payload):
    result = _registry(memory=FakeMemory([], {})).execute(tool, payload)
    assert result.success, result.error


@pytest.mark.parametrize("tool,payload", INVALID_PAYLOADS, ids=lambda v: v if isinstance(v, str) else "")
def test_invalid_payload_is_rejected_before_the_handler_runs(tool, payload):
    def handler_must_not_run(*_a, **_k):
        raise AssertionError("handler ran on invalid input")

    reg = build_default_registry(ToolDependencies(
        retrieval_service=handler_must_not_run, scraping_service=handler_must_not_run,
        vector_memory=handler_must_not_run, verification_agent=handler_must_not_run))
    result = reg.execute(tool, payload)
    assert not result.success and result.output is None
    assert result.error.code == ToolErrorCode.INVALID_INPUT and result.error.details


@pytest.mark.parametrize("tool", list(VALID_PAYLOADS))
def test_unknown_parameters_are_rejected_for_every_tool(tool):
    payload = {**VALID_PAYLOADS[tool], "callback": "os.system('rm -rf /')"}
    result = _registry().execute(tool, payload)
    assert not result.success and result.error.code == ToolErrorCode.INVALID_INPUT
    assert any("callback" in d for d in result.error.details)


def test_error_details_never_echo_input_values():
    secret = "sk-very-secret-value"
    result = _registry().execute("search_web", {"query": "ok query", "max_results": secret})
    assert not result.success
    assert secret not in result.error.model_dump_json()


def test_typed_input_instance_is_accepted():
    retrieval = FakeRetrieval()
    assert _registry(retrieval=retrieval).execute(ToolName.SEARCH_WEB, SearchWebInput(query="typed input")).success


def test_payload_of_the_wrong_kind_is_rejected():
    reg = _registry()
    for bad in (["query"], "query=abc", 42, SearchWebOutput(query="q", research_mode="explore", raw_result_count=0, sources=[])):
        result = reg.execute("search_web", bad)
        assert not result.success and result.error.code == ToolErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "build",
    [
        lambda: SearchWebOutput(query="q", research_mode="explore", raw_result_count=-1, sources=[]),
        lambda: SearchWebOutput(query="q", research_mode="explore", raw_result_count=0, sources=[], extra="x"),
        lambda: SearchWebOutput(query="q", research_mode="nope", raw_result_count=0, sources=[]),
        lambda: SearchWebOutput(query="q", research_mode="explore", raw_result_count=0, sources=[{"title": "t"}]),
        lambda: ExistingEvidenceHit(report_id="r", topic="t", summary="s", similarity_score=1.5),
        lambda: ScrapeSourceOutput(source_id="s", success="maybe-not-a-bool", scraped=None),
        lambda: RequestMoreEvidenceOutput(query="q", research_mode="explore", filtered_already_seen=-1, new_sources=[]),
        lambda: VerifyClaimOutput(claim="c", verification={"verdict": "x", "overall_verification_score": "high"}),
    ],
)
def test_output_models_reject_invalid_data(build):
    with pytest.raises(ValidationError):
        build()


def test_tool_result_invariants():
    out = SearchWebOutput(query="q", research_mode="explore", raw_result_count=0, sources=[])
    assert ToolResult(tool="search_web", success=True, output=out).output is out
    with pytest.raises(ValidationError):
        ToolResult(tool="search_web", success=True)
    with pytest.raises(ValidationError):
        ToolResult(tool="search_web", success=False)
    with pytest.raises(ValidationError):
        ToolResult(tool="search_web", success=True, output=out, error={"code": "unknown_tool", "message": "m"})


def test_successful_result_serialises_the_concrete_output():
    result = _registry().execute("search_web", {"query": "a valid query"})
    dumped = result.model_dump(mode="json")
    assert dumped["success"] is True and len(dumped["output"]["sources"]) == 3


# ── controlled execution ─────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["os.system", "__import__('os')", "eval", "exec", "", "SEARCH_WEB", "search_web ", "search-web", "verify", None, 7])
def test_only_registered_tool_names_execute(name):
    result = _registry().execute(name, {"query": "a valid query"})
    assert not result.success and result.error.code == ToolErrorCode.UNKNOWN_TOOL
    with pytest.raises(UnknownToolError):
        _registry().get(name)


def test_tool_names_can_be_given_as_enum_or_string():
    reg = _registry()
    assert reg.execute(ToolName.SEARCH_WEB, {"query": "a valid query"}).success
    assert reg.execute("search_web", {"query": "a valid query"}).success


def test_handler_exception_is_contained_and_reported():
    class BoomRetrieval:
        def retrieve_and_rank(self, **_):
            raise RuntimeError("tavily exploded")

    result = _registry(retrieval=BoomRetrieval()).execute("search_web", {"query": "a valid query"})
    assert not result.success and result.error.code == ToolErrorCode.EXECUTION_FAILED
    assert "RuntimeError" in result.error.message and "tavily exploded" in result.error.message


def test_overlong_error_messages_are_truncated():
    class BoomRetrieval:
        def retrieve_and_rank(self, **_):
            raise RuntimeError("x" * 5000)

    result = _registry(retrieval=BoomRetrieval()).execute("search_web", {"query": "a valid query"})
    assert len(result.error.message) <= 300


def test_handler_returning_the_wrong_type_is_rejected():
    spec = ToolSpec(ToolName.SEARCH_WEB, "Misbehaving tool used only in this test.",
                    SearchWebInput, SearchWebOutput, handler=lambda inp: {"not": "a model"})
    result = ToolRegistry([spec]).execute("search_web", {"query": "a valid query"})
    assert not result.success and result.error.code == ToolErrorCode.INVALID_OUTPUT


def test_registry_is_closed_and_immutable():
    reg = _registry()
    assert not hasattr(reg, "register") and not hasattr(reg, "add")
    assert isinstance(reg._specs, MappingProxyType)
    with pytest.raises(TypeError):
        reg._specs[ToolName.SEARCH_WEB] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        del reg._specs[ToolName.SEARCH_WEB]  # type: ignore[attr-defined]


def test_registration_rules():
    good = _registry().get(ToolName.SEARCH_WEB)
    with pytest.raises(ValueError):
        ToolRegistry([good, good])  # duplicate
    with pytest.raises(TypeError):
        ToolRegistry([lambda x: x])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        ToolSpec("search_web", "string names are not allowed", SearchWebInput, SearchWebOutput, lambda i: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolSpec(ToolName.SEARCH_WEB, "not a model", dict, SearchWebOutput, lambda i: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolSpec(ToolName.SEARCH_WEB, "handler must be callable", SearchWebInput, SearchWebOutput, "os.system")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolSpec(ToolName.SEARCH_WEB, "   ", SearchWebInput, SearchWebOutput, lambda i: None)
