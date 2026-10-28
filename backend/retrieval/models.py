"""Data structures shared by the hybrid retrieval pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    """One retrievable unit (a web-source snippet, a report chunk, ...).

    ``embedding`` may be pre-computed (e.g. read back from the vector store).
    ``metadata`` is what filters and the reranker's optional priors
    (``credibility`` / ``freshness`` in 0..1) read.
    """

    doc_id: str
    text: str
    title: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    embedding: Optional[Sequence[float]] = None


@dataclass(slots=True)
class RetrievalHit:
    """A retrieved document with the score of every stage that touched it.

    ``score`` is the final ordering score in [0, 1]. ``lexical_score`` is the
    raw BM25 value and ``dense_score`` the raw cosine, so the ranker/evaluator
    can inspect each retriever independently of fusion.
    """

    document: RetrievalDocument
    index: int
    score: float = 0.0
    fused_score: float = 0.0
    lexical_score: float = 0.0
    lexical_rank: Optional[int] = None
    dense_score: Optional[float] = None
    dense_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    rank: int = 0

    @property
    def doc_id(self) -> str:
        return self.document.doc_id


@dataclass(slots=True)
class RetrievalDiagnostics:
    """Why a retrieval returned what it did — surfaced, never hidden.

    ``mode`` is ``hybrid`` (both retrievers contributed), ``lexical_only``,
    ``dense_only`` or ``none``. ``dense_error`` says why dense retrieval was not
    used when it was expected (embedder missing/failed/etc.).
    """

    mode: str = "none"
    dense_used: bool = False
    dense_error: Optional[str] = None
    lexical_used: bool = False
    reranked: bool = False
    diversified: bool = False
    candidates_total: int = 0
    candidates_after_filter: int = 0
    missing_vectors: int = 0
    lexical_hits: int = 0
    dense_hits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(slots=True)
class RetrievalResult:
    hits: list[RetrievalHit]
    diagnostics: RetrievalDiagnostics

    def doc_ids(self) -> list[str]:
        return [hit.doc_id for hit in self.hits]
