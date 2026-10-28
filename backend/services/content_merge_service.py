from __future__ import annotations
from backend.core.logging import get_logger
from backend.models.domain_models import RankedSource, ScrapedContent
logger = get_logger(__name__)


class ContentMergeService:
    def merge(
        self,
        ranked_sources: list[RankedSource],
        scraped_contents: list[ScrapedContent],
    ) -> str:
        # Only successful, non-empty scrapes are usable as full-page content.
        scraped_by_source_id = {
            item.source_id: item
            for item in scraped_contents
            if item.success and item.content.strip()
        }

        sections: list[str] = []
        scraped_count = 0
        fallback_count = 0
        skipped_count = 0

        # Iterate ranked_sources (not scraped_contents) so a source is never
        # silently dropped from the evidence corpus just because scraping
        # failed for it — as long as Tavily already gave us a real,
        # non-empty snippet for that source, that snippet is used instead.
        for ranked in ranked_sources:
            scraped = scraped_by_source_id.get(ranked.source_id)
            if scraped is not None:
                sections.append(
                    "\n".join(
                        [
                            f"Source Title: {scraped.title}",
                            f"URL: {scraped.url}",
                            f"Domain: {scraped.domain}",
                            f"Publish Date: {scraped.publish_date or 'Unknown'}",
                            f"Relevance Score: {ranked.relevance_score}",
                            f"Credibility Score: {ranked.credibility_score}",
                            "Evidence Source: scraped_page",
                            f"Content: {scraped.content}",
                        ]
                    )
                )
                scraped_count += 1
                continue

            if ranked.snippet.strip():
                # Scraping failed or was never attempted for this source, but
                # Tavily already supplied a real (not fabricated) snippet.
                # Use it rather than dropping the source entirely — but label
                # it explicitly as a snippet, never as scraped page content,
                # so verification/writer (and anyone reading the corpus) can
                # tell the difference in evidentiary weight.
                sections.append(
                    "\n".join(
                        [
                            f"Source Title: {ranked.title}",
                            f"URL: {ranked.url}",
                            f"Domain: {ranked.domain}",
                            f"Publish Date: {ranked.publish_date or 'Unknown'}",
                            f"Relevance Score: {ranked.relevance_score}",
                            f"Credibility Score: {ranked.credibility_score}",
                            "Evidence Source: search_snippet_fallback "
                            "(full page scrape unavailable — this is a short "
                            "Tavily search snippet, not full page content)",
                            f"Content: {ranked.snippet}",
                        ]
                    )
                )
                fallback_count += 1
                continue

            # No scraped content and no snippet — nothing usable for this
            # source. It is correctly omitted rather than fabricated.
            skipped_count += 1

        merged = "\n\n---\n\n".join(sections)

        logger.info(
            "Merged scraped content",
            extra={
                "ranked_source_count": len(ranked_sources),
                "scraped_items": len(scraped_contents),
                "scraped_sections": scraped_count,
                "fallback_snippet_sections": fallback_count,
                "skipped_no_content": skipped_count,
                "merged_sections": len(sections),
                "merged_length": len(merged),
            },
        )
        return merged
