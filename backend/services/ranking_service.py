from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse

from rapidfuzz import fuzz


@dataclass(slots=True)
class RetrievedSource:
    title: str
    url: str
    snippet: str
    publish_date: str | None = None


@dataclass(slots=True)
class RankedSourceResult:
    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    publish_date: str | None
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

    def rank_sources(
        self,
        query: str,
        sources: list[RetrievedSource],
    ) -> list[RankedSourceResult]:
        if not query.strip() or not sources:
            return []

        prepared = [self._prepare_source(query, source) for source in sources]
        bm25_values = [item["bm25_score"] for item in prepared]
        cosine_values = [item["cosine_score"] for item in prepared]
        fuzz_values = [item["fuzz_score"] for item in prepared]

        support_map = self._build_support_scores(prepared)

        for item in prepared:
            item["support_score"] = support_map[item["url"]]

            bm25_norm = self._safe_normalize(item["bm25_score"], bm25_values)
            cosine_norm = self._safe_normalize(item["cosine_score"], cosine_values)
            fuzz_norm = self._safe_normalize(item["fuzz_score"], fuzz_values)

            relevance_score = round(
                100 * (
                    0.45 * bm25_norm +
                    0.35 * cosine_norm +
                    0.20 * fuzz_norm
                ),
                2,
            )

            credibility_score = round(
                100 * (
                    0.45 * item["domain_reputation"] +
                    0.20 * item["freshness_score"] +
                    0.20 * (relevance_score / 100.0) +
                    0.15 * item["support_score"]
                ),
                2,
            )

            final_score = round(
                0.7 * relevance_score + 0.3 * credibility_score,
                2,
            )

            item["relevance_score"] = relevance_score
            item["credibility_score"] = credibility_score
            item["final_score"] = final_score

        prepared.sort(key=lambda item: item["final_score"], reverse=True)

        ranked_results: list[RankedSourceResult] = []
        for idx, item in enumerate(prepared, start=1):
            ranked_results.append(
                RankedSourceResult(
                    source_id=item["source_id"],
                    title=item["title"],
                    url=item["url"],
                    domain=item["domain"],
                    snippet=item["snippet"],
                    publish_date=item["publish_date"],
                    bm25_score=round(item["bm25_score"], 4),
                    cosine_score=round(item["cosine_score"], 4),
                    fuzz_score=round(item["fuzz_score"], 4),
                    support_score=round(item["support_score"], 4),
                    relevance_score=item["relevance_score"],
                    credibility_score=item["credibility_score"],
                    final_score=item["final_score"],
                    rank_position=idx,
                )
            )

        return ranked_results

    def _prepare_source(self, query: str, source: RetrievedSource) -> dict:
        query_tokens = self._tokenize(query)
        content = f"{source.title} {source.snippet}".strip()
        content_tokens = self._tokenize(content)
        url_str = str(source.url)
        domain = self._extract_domain(url_str)

        return {
            "source_id": self._build_source_id(url_str),
            "title": source.title,
            "url": url_str,
            "domain": domain,
            "snippet": source.snippet,
            "publish_date": source.publish_date,
            "bm25_score": self._bm25_like_score(query_tokens, content_tokens),
            "cosine_score": self._cosine_similarity(query_tokens, content_tokens),
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

    def _bm25_like_score(self, query_tokens: list[str], doc_tokens: list[str]) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0

        doc_freq = Counter(doc_tokens)
        doc_len = len(doc_tokens)
        avg_doc_len = max(doc_len, 1)
        k1 = 1.5
        b = 0.75
        score = 0.0

        for token in query_tokens:
            freq = doc_freq.get(token, 0)
            if freq == 0:
                continue
            numerator = freq * (k1 + 1)
            denominator = freq + k1 * (1 - b + b * (doc_len / avg_doc_len))
            score += numerator / denominator

        return score

    def _cosine_similarity(self, query_tokens: list[str], doc_tokens: list[str]) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0

        query_counts = Counter(query_tokens)
        doc_counts = Counter(doc_tokens)
        vocabulary = set(query_counts) | set(doc_counts)

        dot = sum(query_counts[t] * doc_counts[t] for t in vocabulary)
        query_norm = math.sqrt(sum(v * v for v in query_counts.values()))
        doc_norm = math.sqrt(sum(v * v for v in doc_counts.values()))

        if query_norm == 0 or doc_norm == 0:
            return 0.0

        return dot / (query_norm * doc_norm)

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

    def _freshness_score(self, publish_date: str | None) -> float:
        if not publish_date:
            return 0.4

        try:
            published = datetime.fromisoformat(publish_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_days = max((now - published).days, 0)

            if age_days <= 30:
                return 1.0
            if age_days <= 90:
                return 0.85
            if age_days <= 180:
                return 0.7
            if age_days <= 365:
                return 0.55
            return 0.35
        except ValueError:
            return 0.4

    def _build_source_id(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"source_{digest}"