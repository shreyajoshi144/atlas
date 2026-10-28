from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable
from urllib.parse import urlparse

from rapidfuzz import fuzz

from backend.core.logging import get_logger
from backend.retrieval import HybridRetriever, RetrievalDocument

logger = get_logger(__name__)


@dataclass(slots=True)
class RetrievedSource:
    title: str
    url: str
    snippet: str
    publish_date: date | str | None = None


@dataclass(slots=True)
class RankedSourceResult:
    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    publish_date: date | str | None
    bm25_score: float
    cosine_score: float
    fuzz_score: float
    support_score: float
    relevance_score: float
    credibility_score: float
    final_score: float
    rank_position: int
    source_type: str = "web_page"


class RankingService:
    DOMAIN_REPUTATION: dict[str, float] = {
        "nature.com": 0.95,
        "science.org": 0.95,
        "nejm.org": 0.95,
        "thelancet.com": 0.95,
        "acm.org": 0.93,
        "ieee.org": 0.93,
        "springer.com": 0.90,
        "arxiv.org": 0.88,
        "openreview.net": 0.87,
        "huggingface.co": 0.86,
        "openai.com": 0.90,
        "anthropic.com": 0.89,
        "deepmind.google": 0.90,
        "research.google": 0.90,
        "microsoft.com": 0.88,
        "learn.microsoft.com": 0.90,
        "aws.amazon.com": 0.88,
        "cloud.google.com": 0.88,
        "docs.python.org": 0.92,
        "developer.mozilla.org": 0.92,
        "pytorch.org": 0.90,
        "tensorflow.org": 0.89,
        "scikit-learn.org": 0.90,
        "kaggle.com": 0.76,
        "github.com": 0.80,
        "github.io": 0.62,
        "wikipedia.org": 0.70,
        "towardsdatascience.com": 0.62,
        "substack.com": 0.50,
        "medium.com": 0.45,
        "dev.to": 0.48,
        "blogspot.com": 0.30,
    }

    def __init__(self, embedding_service=None, *, use_dense: bool = True) -> None:
        # ``embedding_service`` is resolved lazily (shared, self-healing) so that
        # constructing a RankingService never loads a model.
        self._embedding_service = embedding_service
        self._use_dense = use_dense

    def _embedder(self):
        if not self._use_dense:
            return None
        if self._embedding_service is not None:
            return self._embedding_service
        from backend.memory.embedding_service import get_shared_embedding_service

        return get_shared_embedding_service()

    def rank_sources(
        self,
        query: str,
        sources: list[RetrievedSource],
        *,
        freshness_weight: float = 0.20,
    ) -> list[RankedSourceResult]:
        return self.rank_sources_with_diagnostics(query, sources, freshness_weight=freshness_weight)[0]

    def rank_sources_with_diagnostics(
        self,
        query: str,
        sources: list[RetrievedSource],
        *,
        freshness_weight: float = 0.20,
    ):
        """Rank sources; also return the retrieval diagnostics (dense used or not).

        Relevance now comes from the shared hybrid pipeline (real BM25 + dense
        cosine, RRF-fused and reranked). ``bm25_score`` is the raw BM25 value
        and ``cosine_score`` the dense cosine (0.0 when dense embeddings were
        unavailable — see the returned diagnostics).
        """
        from backend.retrieval import RetrievalDiagnostics

        if not query.strip() or not sources:
            return [], RetrievalDiagnostics()

        prepared = [self._prepare_source(query, source) for source in sources]
        documents = [
            RetrievalDocument(doc_id=str(i), text=item["snippet"], title=item["title"])
            for i, item in enumerate(prepared)
        ]
        result = HybridRetriever(self._embedder()).retrieve(query, documents, top_k=len(documents))
        by_index = {int(hit.doc_id): hit for hit in result.hits}
        for i, item in enumerate(prepared):
            hit = by_index.get(i)
            item["bm25_score"] = hit.lexical_score if hit else 0.0
            item["cosine_score"] = max(hit.dense_score, 0.0) if hit and hit.dense_score is not None else 0.0
            item["hybrid_score"] = hit.score if hit else 0.0
        logger.info(
            "Ranked sources",
            extra={"retrieval_mode": result.diagnostics.mode, "dense_used": result.diagnostics.dense_used,
                   "dense_error": result.diagnostics.dense_error},
        )

        fuzz_values = [item["fuzz_score"] for item in prepared]
        support_map = self._build_support_scores(prepared)

        for item in prepared:
            item["support_score"] = support_map[item["url"]]
            fuzz_norm = self._safe_normalize(item["fuzz_score"], fuzz_values)
            relevance_score = round(100 * (0.80 * item["hybrid_score"] + 0.20 * fuzz_norm), 2)

            bounded_freshness_weight = min(max(freshness_weight, 0.0), 0.20)
            remaining = 1.0 - bounded_freshness_weight
            credibility_score = round(
                100 * (
                    (remaining * 0.45) * item["domain_reputation"] +
                    bounded_freshness_weight * item["freshness_score"] +
                    (remaining * 0.20) * (relevance_score / 100.0) +
                    (remaining * 0.15) * item["support_score"]
                ),
                2,
            )
            item["relevance_score"] = relevance_score
            item["credibility_score"] = credibility_score
            item["final_score"] = round(0.7 * relevance_score + 0.3 * credibility_score, 2)

        prepared.sort(key=lambda item: item["final_score"], reverse=True)
        ranked_results = [
            RankedSourceResult(
                source_id=item["source_id"], title=item["title"], url=item["url"],
                domain=item["domain"], snippet=item["snippet"], publish_date=item["publish_date"],
                bm25_score=round(item["bm25_score"], 4), cosine_score=round(item["cosine_score"], 4),
                fuzz_score=round(item["fuzz_score"], 4), support_score=round(item["support_score"], 4),
                relevance_score=item["relevance_score"], credibility_score=item["credibility_score"],
                final_score=item["final_score"], rank_position=idx,
            )
            for idx, item in enumerate(prepared, start=1)
        ]
        return ranked_results, result.diagnostics

    def _prepare_source(self, query: str, source: RetrievedSource) -> dict:
        content = f"{source.title} {source.snippet}".strip()
        url_str = str(source.url)
        domain = self._extract_domain(url_str)
        return {
            "source_id": self._build_source_id(url_str),
            "title": source.title,
            "url": url_str,
            "domain": domain,
            "snippet": source.snippet,
            "publish_date": source.publish_date,
            "bm25_score": 0.0,
            "cosine_score": 0.0,
            "fuzz_score": fuzz.token_sort_ratio(query, content) / 100.0,
            "domain_reputation": self._domain_reputation(domain),
            "freshness_score": self._freshness_score(source.publish_date),
            "support_score": 0.0,
        }

    def _build_support_scores(self, prepared: list[dict]) -> dict[str, float]:
        tokenized_docs: dict[str, set[str]] = {}
        frequency: defaultdict[str, int] = defaultdict(int)

        for item in prepared:
            tokens = set(self._tokenize(f"{item['title']} {item['snippet']}"))
            tokenized_docs[item["url"]] = tokens
            for token in tokens:
                if len(token) >= 4:
                    frequency[token] += 1

        raw_scores: dict[str, float] = {}
        for item in prepared:
            tokens = tokenized_docs[item["url"]]
            if not tokens:
                raw_scores[item["url"]] = 0.0
                continue

            informative_tokens = [t for t in tokens if len(t) >= 4]
            if not informative_tokens:
                raw_scores[item["url"]] = 0.0
                continue

            supported = sum(1 for token in informative_tokens if frequency[token] > 1)
            raw_scores[item["url"]] = supported / len(informative_tokens)

        values = list(raw_scores.values())
        return {url: self._safe_normalize(score, values) for url, score in raw_scores.items()}

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[a-zA-Z0-9]+", text.lower())

    def _safe_normalize(self, value: float, values: Iterable[float]) -> float:
        values = list(values)
        if not values:
            return 0.0

        min_val = min(values)
        max_val = max(values)

        if math.isclose(min_val, max_val):
            return 1.0 if value > 0 else 0.0

        return (value - min_val) / (max_val - min_val)

    def _extract_domain(self, url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc.lower().replace("www.", "")

    def _domain_reputation(self, domain: str) -> float:
        for known, score in self.DOMAIN_REPUTATION.items():
            if domain == known or domain.endswith(f".{known}"):
                return score
        return 0.5

    def _freshness_score(self, publish_date: date | str | None) -> float:
        if not publish_date:
            return 0.4

        try:
            if isinstance(publish_date, datetime):
                published_date = publish_date.date()
            elif isinstance(publish_date, date):
                published_date = publish_date
            else:
                published_date = datetime.fromisoformat(str(publish_date).replace("Z", "+00:00")).date()
            age_days = max((datetime.now(timezone.utc).date() - published_date).days, 0)

            if age_days <= 30:
                return 1.0
            if age_days <= 90:
                return 0.85
            if age_days <= 180:
                return 0.7
            if age_days <= 365:
                return 0.55
            return 0.35
        except (TypeError, ValueError):
            return 0.4

    def _build_source_id(self, url: str) -> str:
        # Include a fresh random component so the same URL cited by two different
        # research runs gets two distinct source_id rows instead of colliding on
        # the sources table's PRIMARY KEY (which silently drops the second one
        # under INSERT OR IGNORE — this was the cause of sources disappearing).
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        unique_suffix = uuid.uuid4().hex[:12]
        return f"source_{digest}_{unique_suffix}"