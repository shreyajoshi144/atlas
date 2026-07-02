from __future__ import annotations

from backend.core.logging import get_logger
from backend.database import ResearchRepository

logger = get_logger(__name__)


class MetricsService:
    def __init__(self, repository: ResearchRepository | None = None) -> None:
        self.repository = repository or ResearchRepository()

    def get_overview(self) -> dict:
        try:
            totals = self.repository.analytics.get_totals()
            total_sources = self.repository.sources.count_all_sources()
            reports_over_time = self.repository.analytics.get_reports_over_time()
            verification_trend = self.repository.analytics.get_verification_trend()
            top_topics = self.repository.analytics.get_top_topics()
            domain_distribution = self.repository.sources.get_domain_distribution()

            return {
                "total_reports_generated": totals.get("total_reports", 0),
                "total_sources_scraped": total_sources,
                "average_verification_score": totals.get("avg_verification_score", 0.0),
                "average_execution_time_ms": totals.get("avg_exec_ms", 0.0),
                "total_tokens_used": totals.get("total_tokens", 0),
                "total_estimated_cost_usd": totals.get("total_cost", 0.0),
                "average_retrieval_latency_ms": totals.get("avg_retrieval_ms", 0.0),
                "reports_generated_over_time": reports_over_time,
                "verification_score_trend": verification_trend,
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
