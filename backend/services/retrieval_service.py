from __future__ import annotations

from backend.core.logging import get_logger
from backend.models.domain_models import RankedSource, SearchResult
from backend.services.ranking_service import RankingService, RetrievedSource
from backend.services.tavily_service import TavilySearchService

logger = get_logger(__name__)


class RetrievalService:
    def __init__(
        self,
        tavily_service: TavilySearchService | None = None,
        ranking_service: RankingService | None = None,
    ) -> None:
        self.tavily_service = tavily_service or TavilySearchService()
        self.ranking_service = ranking_service or RankingService()

    def retrieve_and_rank(
        self,
        query: str,
        top_k: int = 5,
        research_mode: str = "explore",
    ) -> tuple[list[SearchResult], list[RankedSource]]:
        pool_size = max(top_k * 2, 10)
        search_results = self._search_for_mode(query, pool_size, research_mode)

        # Cast HttpUrl → str at the boundary so ranking_service (which uses
        # str operations like .encode() and urlparse) never sees a HttpUrl object.
        retrieved_sources = [
            RetrievedSource(
                title=r.title,
                url=str(r.url),
                snippet=r.snippet,
                publish_date=r.published_date,
            )
            for r in search_results
        ]

        freshness_weight = 0.0 if (research_mode or "explore").lower() == "literature_review" else 0.20
        ranked_results = self.ranking_service.rank_sources(
            query=query,
            sources=retrieved_sources,
            freshness_weight=freshness_weight,
        )

        ranked_sources = [
            RankedSource(
                source_id=r.source_id,
                title=r.title,
                url=str(r.url),   # str() cast keeps Pydantic v2 HttpUrl happy
                domain=r.domain,
                snippet=r.snippet,
                publish_date=r.publish_date,
                bm25_score=r.bm25_score,
                cosine_score=r.cosine_score,
                fuzz_score=r.fuzz_score,
                relevance_score=r.relevance_score,
                credibility_score=r.credibility_score,
                rank_position=r.rank_position,
                source_type=r.source_type,
            )
            for r in ranked_results[:top_k]
        ]

        logger.info(
            "Retrieved and ranked sources",
            extra={
                "query": query,
                "search_result_count": len(search_results),
                "ranked_count": len(ranked_sources),
            },
        )
        return search_results, ranked_sources

    def _search_for_mode(self, query: str, pool_size: int, research_mode: str) -> list[SearchResult]:
        """Use Tavily recency filters as a retrieval preference, not a hard global cutoff.

        Recent results are fetched for modes where freshness matters, then a normal
        unfiltered search is merged in so authoritative/foundational sources remain
        eligible. Literature review intentionally uses only the unfiltered search.
        """
        mode = (research_mode or "explore").strip().lower()
        if mode == "literature_review":
            return self.tavily_service.search(query=query, max_results=pool_size)

        time_range_by_mode = {
            "explore": "year",
            "compare": "month",
            "investigate": "month",
            "brief": "month",
        }
        recent_window = time_range_by_mode.get(mode)
        if not recent_window:
            return self.tavily_service.search(query=query, max_results=pool_size)

        recent_limit = max(1, pool_size // 2)
        recent = self.tavily_service.search(
            query=query,
            max_results=recent_limit,
            time_range=recent_window,
        )
        broad = self.tavily_service.search(query=query, max_results=pool_size)

        merged: list[SearchResult] = []
        seen: set[str] = set()
        for result in [*recent, *broad]:
            key = str(result.url).rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(result)
            if len(merged) >= pool_size:
                break

        return merged

