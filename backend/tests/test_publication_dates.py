from __future__ import annotations

import sys
import types
from datetime import date

# The test environment may omit tavily-python for offline unit tests. Stub only
# the SDK import; retrieval behavior is exercised with a fake Tavily service.
if "tavily" not in sys.modules:
    tavily_stub = types.ModuleType("tavily")
    tavily_stub.TavilyClient = object
    sys.modules["tavily"] = tavily_stub

from backend.models.domain_models import SearchResult
from backend.services.ranking_service import RankingService, RetrievedSource
from backend.services.retrieval_service import RetrievalService
from backend.services.scraping_service import AsyncScrapingService


def test_search_result_parses_tavily_publication_date_safely():
    result = SearchResult(
        title="Source",
        url="https://example.com/source",
        snippet="content",
        domain="example.com",
        rank_position=1,
        published_date="2026-09-18T12:30:00Z",
    )
    assert result.published_date == date(2026, 9, 18)

    unknown = result.model_copy(update={"published_date": "not-a-date"})
    # model_copy does not rerun validators; construct a fresh model instead.
    unknown = SearchResult(
        title="Source",
        url="https://example.com/source",
        snippet="content",
        domain="example.com",
        rank_position=1,
        published_date="not-a-date",
    )
    assert unknown.published_date is None


def test_retrieval_preserves_dates_and_uses_mode_recency_controls():
    class FakeTavily:
        def __init__(self):
            self.calls = []

        def search(self, query, max_results=None, *, time_range=None):
            self.calls.append(time_range)
            return [
                SearchResult(
                    title="Recent",
                    url="https://example.com/recent",
                    snippet="current evidence",
                    domain="example.com",
                    rank_position=1,
                    published_date="2026-09-18",
                ),
                SearchResult(
                    title="Foundational",
                    url="https://example.com/foundational",
                    snippet="foundational evidence",
                    domain="example.com",
                    rank_position=2,
                    published_date="2019-01-01",
                ),
            ][: max_results or 2]

    for mode, expected_window in {
        "explore": "year",
        "compare": "month",
        "investigate": "month",
        "brief": "month",
        "literature_review": None,
    }.items():
        fake = FakeTavily()
        _, ranked = RetrievalService(tavily_service=fake).retrieve_and_rank(
            "nuclear energy",
            top_k=2,
            research_mode=mode,
        )
        if expected_window is None:
            assert fake.calls == [None]
        else:
            assert expected_window in fake.calls
            assert None in fake.calls  # broad fallback retains older sources
        assert all(source.publish_date is not None for source in ranked)


def test_ranking_freshness_is_bounded_and_does_not_replace_relevance():
    ranked = RankingService().rank_sources(
        "nuclear energy",
        [
            RetrievedSource("Recent", "https://example.com/recent", "nuclear energy", "2026-09-18"),
            RetrievedSource("Old", "https://example.com/old", "nuclear energy", "2019-01-01"),
        ],
        freshness_weight=0.20,
    )
    assert all(0 <= item.credibility_score <= 100 for item in ranked)
    assert all(item.publish_date for item in ranked)


def test_scraper_uses_structured_publication_metadata_and_prefers_tavily_date():
    scraper = AsyncScrapingService.__new__(AsyncScrapingService)
    scraper.max_chars = 2200
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"NewsArticle","datePublished":"2026-09-17T10:00:00Z",'
        '"dateModified":"2026-09-20T10:00:00Z"}'
        '</script></head><body><article><p>'
        + ("content " * 100)
        + "</p></article></body></html>"
    )

    parsed = scraper._extract_content(html, "https://example.com", "Fallback", "example.com", {}, None)
    assert parsed["publish_date"] == date(2026, 9, 17)

    tavily_date = date(2026, 9, 16)
    parsed_with_tavily = scraper._extract_content(
        html, "https://example.com", "Fallback", "example.com", {}, tavily_date
    )
    assert parsed_with_tavily["publish_date"] == tavily_date


def test_scraper_never_uses_last_modified_as_publication_date():
    scraper = AsyncScrapingService.__new__(AsyncScrapingService)
    scraper.max_chars = 2200
    html = '<html><body><article><p>' + ("content " * 100) + "</p></article></body></html>"
    parsed = scraper._extract_content(
        html,
        "https://example.com",
        "Fallback",
        "example.com",
        {"Last-Modified": "Fri, 18 Sep 2026 12:00:00 GMT"},
        None,
    )
    assert parsed["publish_date"] is None



def test_scrape_node_propagates_scraper_date_by_source_id_without_overwriting_tavily_date(monkeypatch):
    import importlib
    import sys
    import types

    # Avoid loading the optional LangGraph workflow just to unit-test the node.
    workflow_stub = types.ModuleType("backend.graph.workflow")
    workflow_stub.build_research_graph = lambda: None
    sys.modules.setdefault("backend.graph.workflow", workflow_stub)
    nodes = importlib.import_module("backend.graph.nodes")
    from backend.models.common import MetricBreakdown
    from backend.models.domain_models import RankedSource, ScrapedContent

    tavily_date = date(2026, 9, 16)
    scraper_date = date(2026, 9, 17)

    ranked_sources = [
        RankedSource(
            source_id="src-known",
            title="Known date",
            url="https://example.com/known",
            domain="example.com",
            publish_date=tavily_date,
        ),
        RankedSource(
            source_id="src-missing",
            title="Missing date",
            url="https://example.com/missing",
            domain="example.com",
            publish_date=None,
        ),
        RankedSource(
            source_id="src-none",
            title="No date",
            url="https://example.com/none",
            domain="example.com",
            publish_date=None,
        ),
    ]
    scraped_contents = [
        ScrapedContent(
            source_id="src-known",
            url="https://example.com/known",
            title="Known date",
            domain="example.com",
            publish_date=scraper_date,
            content="content",
            content_hash="known",
        ),
        ScrapedContent(
            source_id="src-missing",
            url="https://example.com/missing",
            title="Different title entirely",
            domain="example.com",
            publish_date=scraper_date,
            content="content",
            content_hash="missing",
        ),
        ScrapedContent(
            source_id="unrelated-id",
            url="https://example.com/none",
            title="No date",
            domain="example.com",
            publish_date=date(2026, 9, 18),
            content="content",
            content_hash="unrelated",
        ),
    ]

    class FakeScraper:
        async def scrape_sources(self, sources):
            return scraped_contents

    monkeypatch.setattr(nodes, "_scraping_service", lambda: FakeScraper())
    state = {
        "ranked_sources": ranked_sources,
        "metrics": MetricBreakdown(),
    }

    result = nodes.scrape_node(state)
    updated = {source.source_id: source for source in result["ranked_sources"]}

    assert updated["src-known"].publish_date == tavily_date
    assert updated["src-missing"].publish_date == scraper_date
    assert updated["src-none"].publish_date is None
