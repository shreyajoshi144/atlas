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
    ) -> tuple[list[SearchResult], list[RankedSource]]:
        search_results = self.tavily_service.search(query=query, max_results=max(top_k * 2, 10))

        # Cast HttpUrl → str at the boundary so ranking_service (which uses
        # str operations like .encode() and urlparse) never sees a HttpUrl object.
        retrieved_sources = [
            RetrievedSource(
                title=r.title,
                url=str(r.url),
                snippet=r.snippet,
                publish_date=None,
            )
            for r in search_results
        ]

        ranked_results = self.ranking_service.rank_sources(
            query=query,
            sources=retrieved_sources,
        )

        ranked_sources = [
            RankedSource(
                source_id=r.source_id,
                title=r.title,
                url=str(r.url),   # str() cast keeps Pydantic v2 HttpUrl happy
                domain=r.domain,
                snippet=r.snippet,
                publish_date=None,
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
