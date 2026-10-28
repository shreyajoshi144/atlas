from __future__ import annotations

from backend.core.logging import get_logger
from backend.database import ResearchRepository

logger = get_logger(__name__)


def _normalise_score(score) -> float:
    """Domain model stores verification scores 0-100; the frontend expects 0-1
    everywhere (this mirrors the same normalisation api/routes/history.py applies —
    analytics was reading the raw 0-100 value directly, which is why the overview
    page showed things like '8200%' and an overflowing verification-trend chart)."""
    if score is None:
        return 0.0
    score = float(score)
    return round(score / 100.0, 4) if score > 1.0 else round(score, 4)


class MetricsService:
    def __init__(self, repository: ResearchRepository | None = None) -> None:
        self.repository = repository or ResearchRepository()

    def get_overview(self, user_id: str | None = None) -> dict:
        try:
            totals = self.repository.analytics.get_totals(user_id=user_id)
            total_sources = self.repository.sources.count_all_sources(user_id=user_id)
            reports_over_time = self.repository.analytics.get_reports_over_time(user_id=user_id)
            verification_trend = self.repository.analytics.get_verification_trend(user_id=user_id)
            top_topics = self.repository.analytics.get_top_topics(user_id=user_id)
            domain_distribution = self.repository.sources.get_domain_distribution(user_id=user_id)

            normalised_trend = [
                {**point, "avg_score": _normalise_score(point.get("avg_score"))}
                for point in verification_trend
            ]

            return {
                "total_reports_generated": totals.get("total_reports", 0),
                "total_sources_scraped": total_sources,
                "average_verification_score": _normalise_score(totals.get("avg_verification_score", 0.0)),
                "average_execution_time_ms": totals.get("avg_exec_ms", 0.0),
                "total_tokens_used": totals.get("total_tokens", 0),
                "total_estimated_cost_usd": totals.get("total_cost", 0.0),
                "average_retrieval_latency_ms": totals.get("avg_retrieval_ms", 0.0),
                "reports_generated_over_time": reports_over_time,
                "verification_score_trend": normalised_trend,
                "top_topics": top_topics,
                "source_domain_distribution": domain_distribution,
            }
        except Exception as exc:
            logger.error("MetricsService.get_overview failed", extra={"error": str(exc)})
            return {
                "total_reports_generated": 0,
                "total_sources_scraped": 0,
                "average_verification_score": 0.0,
                "average_execution_time_ms": 0.0,
                "total_tokens_used": 0,
                "total_estimated_cost_usd": 0.0,
                "average_retrieval_latency_ms": 0.0,
                "reports_generated_over_time": [],
                "verification_score_trend": [],
                "top_topics": [],
                "source_domain_distribution": [],
            }
