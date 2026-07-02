from __future__ import annotations
from datetime import date, datetime, timezone
from backend.core.logging import get_logger
from backend.models.domain_models import RankedSource
logger = get_logger(__name__)

class CredibilityScoringService:
    DOMAIN_REPUTATION = {
        "nature.com": 95.0,
        "science.org": 93.0,
        "nih.gov": 92.0,
        "who.int": 91.0,
        "oecd.org": 90.0,
        "worldbank.org": 89.0,
        "arxiv.org": 82.0,
        "github.com": 75.0,
        "wikipedia.org": 70.0,
        "medium.com": 45.0,
        "substack.com": 40.0,
    }

    def score_sources(self, ranked_sources: list[RankedSource]) -> list[RankedSource]:
        support_count = max(len(ranked_sources), 1)

        for source in ranked_sources:
            domain_score = self._score_domain_reputation(source.domain)
            freshness_score = self._score_freshness(source.publish_date)
            relevance_score = min(max(source.relevance_score, 0.0), 100.0)
            supporting_sources_score = min(100.0, (support_count / 5.0) * 100.0)

            final_score = (
                0.35 * domain_score
                + 0.20 * freshness_score
                + 0.30 * relevance_score
                + 0.15 * supporting_sources_score
            )

            source.credibility_score = round(min(max(final_score, 0.0), 100.0), 2)

        logger.info(
            "Credibility scoring completed",
            extra={"source_count": len(ranked_sources)},
        )
        return ranked_sources

    def _score_domain_reputation(self, domain: str) -> float:
        normalized = domain.lower().replace("www.", "")
        for known_domain, score in self.DOMAIN_REPUTATION.items():
            if normalized.endswith(known_domain):
                return score
        if normalized.endswith(".gov") or normalized.endswith(".edu"):
            return 85.0
        if normalized.endswith(".org"):
            return 68.0
        return 50.0

    def _score_freshness(self, publish_date: date | None) -> float:
        if not publish_date:
            return 40.0

        today = datetime.now(timezone.utc).date()
        age_days = max((today - publish_date).days, 0)

        if age_days <= 30:
            return 100.0
        if age_days <= 90:
            return 85.0
        if age_days <= 180:
            return 70.0
        if age_days <= 365:
            return 55.0
        if age_days <= 730:
            return 40.0
        return 25.0