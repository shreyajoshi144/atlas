"""Hybrid retrieval: BM25 + dense similarity, fused, filtered and reranked.

Pipeline for one query over a candidate set::

    metadata filter ─► BM25 ranking ─┐
                                     ├─► RRF fusion ─► rerank (+MMR) ─► top-k
    dense cosine ranking ────────────┘

Dense retrieval is *optional per call*. If the embedder is missing, unavailable
or raises, the call degrades to lexical-only and says so in
``RetrievalDiagnostics`` — vectors are never fabricated.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence

from backend.core.logging import get_logger
from backend.retrieval.bm25 import BM25Index
from backend.retrieval.config import HybridConfig, load_hybrid_config
from backend.retrieval.dense import cosine_similarity
from backend.retrieval.filters import matches_filter, validate_filter
from backend.retrieval.fusion import max_possible_rrf, reciprocal_rank_fusion
from backend.retrieval.models import (
    RetrievalDiagnostics,
    RetrievalDocument,
    RetrievalHit,
    RetrievalResult,
)
from backend.retrieval.rerank import FeatureReranker, Reranker
from backend.retrieval.text import lexical_terms

logger = get_logger(__name__)


class Embedder(Protocol):
    """What the retriever needs from an embedding backend.

    ``backend.memory.EmbeddingService`` satisfies this; tests inject fakes.
    ``embed_texts`` must raise if it cannot produce vectors.
    """

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class HybridRetriever:
    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        *,
        reranker: Optional[Reranker] = None,
        config: Optional[HybridConfig] = None,
    ) -> None:
        self.embedder = embedder
        self.config = config or load_hybrid_config()
        self._reranker = reranker  # None => a FeatureReranker built per call

    # ── public API ───────────────────────────────────────────────────────────
    def retrieve(
        self,
        query: str,
        documents: Sequence[RetrievalDocument],
        *,
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
        dense_query: Optional[str] = None,
        rerank: bool = True,
        diversify: bool = False,
        use_lexical: bool = True,
        use_dense: bool = True,
        embed_missing: bool = True,
    ) -> RetrievalResult:
        """Retrieve the ``top_k`` documents for ``query``.

        ``dense_query`` lets callers embed a richer string (e.g. with chat
        history) while BM25 sees only the focused ``query``. ``use_lexical`` /
        ``use_dense`` exist so the benchmark can ablate each retriever.
        ``embed_missing=False`` skips on-the-fly embedding of documents that
        have no stored vector (used for the persisted vector store, where a
        missing vector means index drift, not "please embed it now").
        """
        diag = RetrievalDiagnostics(candidates_total=len(documents))
        validate_filter(filters)  # a malformed filter must fail even on empty input
        if top_k <= 0 or not (query or "").strip() or not documents:
            return RetrievalResult(hits=[], diagnostics=diag)

        candidates = [
            (i, d) for i, d in enumerate(documents) if matches_filter(d.metadata, filters)
        ]
        diag.candidates_after_filter = len(candidates)
        if not candidates:
            return RetrievalResult(hits=[], diagnostics=diag)

        cfg = self.config
        local = [d for _, d in candidates]
        hits = [RetrievalHit(document=d, index=pos) for pos, d in enumerate(local)]

        rankings: dict[str, list[int]] = {}
        weights: dict[str, float] = {}

        # ── lexical ──────────────────────────────────────────────────────────
        if use_lexical:
            doc_terms = [lexical_terms(f"{d.title} {d.title} {d.text}") for d in local]  # title counted twice
            index = BM25Index(doc_terms)
            scores = index.score(lexical_terms(query))
            positive = [(i, s) for i, s in enumerate(scores) if s > 0.0]
            positive.sort(key=lambda p: (-p[1], p[0]))
            for rank, (i, s) in enumerate(positive[: cfg.candidate_depth], start=1):
                hits[i].lexical_score = s
                hits[i].lexical_rank = rank
            rankings["lexical"] = [i for i, _ in positive[: cfg.candidate_depth]]
            weights["lexical"] = cfg.lexical_weight
            diag.lexical_hits = len(rankings["lexical"])
            diag.lexical_used = bool(rankings["lexical"])

        # ── dense ────────────────────────────────────────────────────────────
        if use_dense:
            dense_ranking = self._dense_stage(dense_query or query, local, hits, diag, embed_missing)
            if dense_ranking:
                rankings["dense"] = dense_ranking
                weights["dense"] = cfg.dense_weight
                diag.dense_hits = len(dense_ranking)
                diag.dense_used = True

        active = {name: order for name, order in rankings.items() if order}
        if not active:
            return RetrievalResult(hits=[], diagnostics=diag)

        diag.mode = (
            "hybrid" if len(active) == 2
            else "lexical_only" if "lexical" in active
            else "dense_only"
        )

        # ── fusion ───────────────────────────────────────────────────────────
        fused = reciprocal_rank_fusion(active, weights=weights, k=cfg.rrf_k)
        for i, value in fused.items():
            hits[i].fused_score = value
        max_fused = max_possible_rrf([weights[n] for n in active], cfg.rrf_k)

        shortlist = sorted(fused, key=lambda i: (-fused[i], i))[: max(cfg.rerank_depth, top_k)]
        shortlisted = [hits[i] for i in shortlist]

        # ── rerank / finalise ────────────────────────────────────────────────
        if rerank:
            reranker = self._reranker or FeatureReranker(
                diversify=diversify, mmr_lambda=cfg.mmr_lambda, top_k=top_k
            )
            ordered = reranker.rerank(query, shortlisted, max_fused=max_fused)
            diag.reranked = True
            diag.diversified = bool(getattr(reranker, "diversify", False))
        else:
            for h in shortlisted:
                h.score = min(h.fused_score / max_fused, 1.0) if max_fused else 0.0
            ordered = sorted(shortlisted, key=lambda h: (-h.score, h.index))

        final = ordered[:top_k]
        for rank, h in enumerate(final, start=1):
            h.rank = rank
        return RetrievalResult(hits=final, diagnostics=diag)

    # ── internals ────────────────────────────────────────────────────────────
    def _dense_stage(
        self,
        dense_query: str,
        local: list[RetrievalDocument],
        hits: list[RetrievalHit],
        diag: RetrievalDiagnostics,
        embed_missing: bool,
    ) -> list[int]:
        embedder = self.embedder
        if embedder is None:
            diag.dense_error = "no_embedder"
            return []
        # No `is_available` shortcut on purpose: an embedder that cannot embed
        # raises, and that is caught below and reported in the diagnostics.
        try:
            query_vec = embedder.embed_texts([dense_query])[0]
            if not query_vec or not any(query_vec):
                diag.dense_error = "empty_query_embedding"
                return []

            vectors: list[Optional[Sequence[float]]] = [d.embedding for d in local]
            missing = [i for i, v in enumerate(vectors) if not v]
            if missing and embed_missing:
                embedded = embedder.embed_texts([f"{local[i].title}\n{local[i].text}".strip() for i in missing])
                if len(embedded) != len(missing):
                    raise ValueError("embedder returned a different number of vectors than requested")
                for i, vec in zip(missing, embedded):
                    vectors[i] = vec
        except Exception as exc:  # embedding backend failure => degrade, don't crash
            diag.dense_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Dense retrieval unavailable — falling back to lexical-only",
                extra={"error": diag.dense_error},
            )
            return []

        scored: list[tuple[int, float]] = []
        for i, vec in enumerate(vectors):
            cos = cosine_similarity(query_vec, vec)
            if cos is None:
                diag.missing_vectors += 1
                continue
            hits[i].dense_score = cos
            if cos >= self.config.min_dense_similarity:
                scored.append((i, cos))
        scored.sort(key=lambda p: (-p[1], p[0]))
        return [i for i, _ in scored[: self.config.candidate_depth]]
