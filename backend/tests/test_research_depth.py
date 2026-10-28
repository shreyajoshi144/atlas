from pathlib import Path

import pytest

from backend.models.api_models import ResearchRunRequest, SettingsUpdateRequest
from backend.api.routes.research import _resolve_top_k_urls
from backend.models.domain_models import SearchResult
from backend.services.retrieval_service import RetrievalService


MODE_TARGETS = {
    "brief": 8,
    "explore": 12,
    "compare": 14,
    "investigate": 20,
    "literature_review": 20,
}


class FakeTavily:
    def __init__(self, count: int):
        self.count = count
        self.calls = []

    def search(self, query, max_results=None, *, time_range=None):
        self.calls.append((query, max_results, time_range))
        n = min(self.count, max_results or self.count)
        return [
            SearchResult(
                title=f"Source {i}",
                url=f"https://example.com/source-{i}",
                snippet="evidence",
                domain="example.com",
                rank_position=i,
            )
            for i in range(n)
        ]


class FakeRanker:
    """Stands in for RankingService but preserves its real output contract:
    rank_sources() returns RankedSourceResult objects (source_id, domain,
    scores, etc.), never the plain RetrievedSource objects it was given —
    retrieve_and_rank()'s RankedSource(...) construction reads those fields
    directly. A fake that just echoed the input `sources` back out was
    testing against a contract the real ranking_service.py never has.
    """

    def rank_sources(self, query, sources, freshness_weight=0.0):
        from backend.services.ranking_service import RankingService

        return RankingService().rank_sources(query, sources, freshness_weight=freshness_weight)


@pytest.mark.parametrize("mode,target", MODE_TARGETS.items())
def test_mode_targets_reach_retrieve_and_rank(mode, target):
    fake = FakeTavily(target)
    service = RetrievalService(tavily_service=fake, ranking_service=FakeRanker())
    captured = {}

    original = service.ranking_service.rank_sources

    def capture(query, sources, freshness_weight=0.0):
        captured["count"] = len(sources)
        return original(query, sources, freshness_weight)

    service.ranking_service.rank_sources = capture
    _, ranked = service.retrieve_and_rank("depth test", top_k=target, research_mode=mode)

    assert captured["count"] == target
    assert len(ranked) == target


def test_twenty_requested_with_seven_unique_sources_returns_seven_without_duplicates():
    fake = FakeTavily(7)
    service = RetrievalService(tavily_service=fake, ranking_service=FakeRanker())
    _, ranked = service.retrieve_and_rank("depth test", top_k=20, research_mode="investigate")

    assert len(ranked) == 7
    assert len({str(source.url) for source in ranked}) == 7


def test_top_k_20_is_not_reduced_to_5_or_10():
    fake = FakeTavily(20)
    service = RetrievalService(tavily_service=fake, ranking_service=FakeRanker())
    _, ranked = service.retrieve_and_rank("depth test", top_k=20, research_mode="investigate")

    assert len(ranked) == 20
    assert any(call[1] == 40 for call in fake.calls)


def test_api_allows_top_k_20_and_settings_allow_20():
    assert ResearchRunRequest(topic="depth", top_k_urls=20).top_k_urls == 20
    assert SettingsUpdateRequest(default_top_k_urls=20).default_top_k_urls == 20


def test_frontend_mode_targets_and_settings_ceiling():
    script = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "script.js").read_text()
    assert 'value="brief" data-k="8"' in script
    assert 'value="explore" data-k="12"' in script
    assert 'value="compare" data-k="14"' in script
    assert 'value="investigate" data-k="20"' in script
    assert 'value="literature_review" data-k="20"' in script
    assert 'id="setTopK" type="number" min="1" max="20"' in script


def test_api_omitted_top_k_uses_mode_target_without_overriding_explicit_value():
    assert _resolve_top_k_urls(ResearchRunRequest(topic="depth", research_mode="explore")) == 12
    assert _resolve_top_k_urls(ResearchRunRequest(topic="depth", research_mode="investigate")) == 20
    assert _resolve_top_k_urls(ResearchRunRequest(topic="depth", research_mode="brief")) == 8
    assert _resolve_top_k_urls(ResearchRunRequest(topic="depth", research_mode="compare", top_k_urls=7)) == 7
