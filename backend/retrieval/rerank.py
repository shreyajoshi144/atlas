"""Second-stage reranking.

First-stage retrieval (BM25 + dense, fused by RRF) is recall-oriented and
rank-based. The reranker re-scores the short fused list with *absolute*
features the first stage cannot see — how much of the query a document really
covers, adjacent-phrase matches, title matches, the dense similarity itself and
optional source priors — then optionally diversifies with MMR so near-duplicate
passages do not crowd out other evidence.

``Reranker`` is a protocol: a cross-encoder can be dropped in later without
touching the retriever.
"""
from __future__ import annotations

from typing import Callable, Protocol, Sequence

from backend.retrieval.dense import cosine_similarity
from backend.retrieval.models import RetrievalHit
from backend.retrieval.text import STOPWORDS, tokenize


class Reranker(Protocol):
    def rerank(self, query: str, hits: Sequence[RetrievalHit], *, max_fused: float) -> list[RetrievalHit]:
        """Return ``hits`` with ``rerank_score``/``score`` set, best first."""


def _unique(seq: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def _bigrams(tokens: Sequence[str]) -> set[tuple[str, str]]:
    return {
        (a, b)
        for a, b in zip(tokens, tokens[1:])
        if not (a in STOPWORDS and b in STOPWORDS)
    }


def _contains_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - n + 1):
        if haystack[i] == first and list(haystack[i : i + n]) == list(needle):
            return True
    return False


class FeatureReranker:
    """Deterministic feature-based reranker with optional MMR diversification."""

    # feature -> base weight. ``dense`` and ``prior`` are dropped (and the rest
    # renormalised) when a hit has no such signal, so scores stay comparable.
    WEIGHTS = {
        "fused": 0.30,
        "dense": 0.20,
        "coverage": 0.20,
        "bigram": 0.08,
        "title": 0.08,
        "phrase": 0.04,
        "prior": 0.10,
    }

    def __init__(self, *, diversify: bool = False, mmr_lambda: float = 0.7, top_k: int | None = None) -> None:
        self.diversify = diversify
        self.mmr_lambda = mmr_lambda
        self.top_k = top_k

    def rerank(self, query: str, hits: Sequence[RetrievalHit], *, max_fused: float) -> list[RetrievalHit]:
        q_tokens = tokenize(query)
        q_terms = _unique(tokenize(query, remove_stopwords=True))
        q_bigrams = _bigrams(q_tokens)
        phrase_tokens = tokenize(query, remove_stopwords=False)

        for hit in hits:
            doc = hit.document
            body_tokens = tokenize(doc.text)
            body_set = set(body_tokens)
            title_tokens = tokenize(doc.title)
            title_set = set(title_tokens)
            all_set = body_set | title_set

            coverage = (sum(1 for t in q_terms if t in all_set) / len(q_terms)) if q_terms else 0.0
            title_cov = (sum(1 for t in q_terms if t in title_set) / len(q_terms)) if q_terms and title_set else 0.0
            doc_bigrams = _bigrams(body_tokens) | _bigrams(title_tokens)
            bigram = (len(q_bigrams & doc_bigrams) / len(q_bigrams)) if q_bigrams else 0.0
            phrase = 1.0 if len(phrase_tokens) > 1 and _contains_sequence(body_tokens, phrase_tokens) else 0.0
            fused = min(hit.fused_score / max_fused, 1.0) if max_fused > 0 else 0.0

            features = {"fused": fused, "coverage": coverage, "bigram": bigram, "title": title_cov, "phrase": phrase}
            if hit.dense_score is not None:
                features["dense"] = max(0.0, min(hit.dense_score, 1.0))
            priors = [
                float(doc.metadata[k]) for k in ("credibility", "freshness")
                if isinstance(doc.metadata.get(k), (int, float)) and not isinstance(doc.metadata.get(k), bool)
            ]
            if priors:
                features["prior"] = max(0.0, min(sum(priors) / len(priors), 1.0))

            total_weight = sum(self.WEIGHTS[name] for name in features)
            score = sum(self.WEIGHTS[name] * value for name, value in features.items()) / total_weight
            hit.rerank_score = round(score, 6)
            hit.score = hit.rerank_score

        ordered = sorted(hits, key=lambda h: (-(h.rerank_score or 0.0), h.index))
        if self.diversify:
            ordered = self._mmr(ordered, self.top_k or len(ordered))
        return ordered

    def _mmr(self, ordered: list[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        """Maximal Marginal Relevance: trade relevance against redundancy."""
        similarity = self._similarity_fn()
        remaining = list(ordered)
        selected: list[RetrievalHit] = []
        lam = self.mmr_lambda
        while remaining and len(selected) < top_k:
            if not selected:
                selected.append(remaining.pop(0))
                continue
            best_idx, best_val = 0, float("-inf")
            for i, cand in enumerate(remaining):
                redundancy = max(similarity(cand, s) for s in selected)
                value = lam * (cand.rerank_score or 0.0) - (1.0 - lam) * redundancy
                if value > best_val:
                    best_idx, best_val = i, value
            selected.append(remaining.pop(best_idx))
        return selected

    @staticmethod
    def _similarity_fn() -> Callable[[RetrievalHit, RetrievalHit], float]:
        token_cache: dict[int, frozenset[str]] = {}

        def tokens(hit: RetrievalHit) -> frozenset[str]:
            if hit.index not in token_cache:
                token_cache[hit.index] = frozenset(tokenize(hit.document.text, remove_stopwords=True))
            return token_cache[hit.index]

        def sim(a: RetrievalHit, b: RetrievalHit) -> float:
            cos = cosine_similarity(a.document.embedding, b.document.embedding)
            if cos is not None:
                return max(0.0, cos)
            ta, tb = tokens(a), tokens(b)
            if not ta or not tb:
                return 0.0
            return len(ta & tb) / len(ta | tb)

        return sim
