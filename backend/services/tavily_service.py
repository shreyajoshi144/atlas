from __future__ import annotations
from urllib.parse import urlparse
from tavily import TavilyClient
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.domain_models import SearchResult

logger = get_logger(__name__)

class TavilySearchService:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.client = TavilyClient(api_key=settings.tavily_api_key)

    def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        limit = max_results or self.settings.tavily_max_results
        logger.info(
            "Starting Tavily search",
            extra={"query": query, "max_results": limit},
        )

        response = self.client.search(
            query=query,
            max_results=limit,
            search_depth="advanced",
            include_answer=False,
            include_raw_content=False,
        )

        results: list[SearchResult] = []
        for idx, item in enumerate(response.get("results", []), start=1):
            url = item.get("url", "").strip()
            if not url:
                continue

            domain = self._extract_domain(url)
            results.append(
                SearchResult(
                    title=item.get("title", "").strip() or domain,
                    url=url,
                    snippet=item.get("content", "").strip(),
                    domain=domain,
                    rank_position=idx,
                )
            )

        logger.info(
            "Completed Tavily search",
            extra={"query": query, "result_count": len(results)},
        )
        return results

    @staticmethod
    def _extract_domain(url: str) -> str:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return domain.replace("www.", "")