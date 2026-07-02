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
        source_map = {item.source_id: item for item in ranked_sources}
        sections: list[str] = []

        for scraped in scraped_contents:
            ranked = source_map.get(scraped.source_id)
            if not ranked or not scraped.success or not scraped.content.strip():
                continue

            sections.append(
                "\n".join(
                    [
                        f"Source Title: {scraped.title}",
                        f"URL: {scraped.url}",
                        f"Domain: {scraped.domain}",
                        f"Publish Date: {scraped.publish_date or 'Unknown'}",
                        f"Relevance Score: {ranked.relevance_score}",
                        f"Credibility Score: {ranked.credibility_score}",
                        f"Content: {scraped.content}",
                    ]
                )
            )

        merged = "\n\n---\n\n".join(sections)

        logger.info(
            "Merged scraped content",
            extra={
                "scraped_items": len(scraped_contents),
                "merged_sections": len(sections),
                "merged_length": len(merged),
            },
        )
        return merged