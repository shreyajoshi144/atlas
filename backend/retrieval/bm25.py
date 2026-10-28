"""Okapi BM25 with real inverse-document-frequency and length normalisation.

This replaces the M4 ``_bm25_like_score`` which had two defects: it computed no
IDF at all, and it set ``avg_doc_len`` to the document's own length so the
length-normalisation term was a constant 1.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence


class BM25Index:
    """Immutable BM25 index over pre-tokenised documents."""

    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 < 0 or not 0.0 <= b <= 1.0:
            raise ValueError("BM25 requires k1 >= 0 and 0 <= b <= 1")
        self.k1 = k1
        self.b = b
        self.size = len(documents)
        self._doc_lengths = [len(doc) for doc in documents]
        total = sum(self._doc_lengths)
        self.avg_doc_length = (total / self.size) if self.size and total else 0.0
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for index, doc in enumerate(documents):
            for term, freq in Counter(doc).items():
                self._postings.setdefault(term, []).append((index, freq))

    def document_frequency(self, term: str) -> int:
        return len(self._postings.get(term, ()))

    def idf(self, term: str) -> float:
        """Lucene-style IDF: always positive, so common terms never subtract."""
        n = self.document_frequency(term)
        return math.log(1.0 + (self.size - n + 0.5) / (n + 0.5))

    def score(self, query_terms: Sequence[str]) -> list[float]:
        """BM25 score of every indexed document for ``query_terms``."""
        scores = [0.0] * self.size
        if not self.size or not self.avg_doc_length:
            return scores
        for term in dict.fromkeys(query_terms):  # unique, order-preserving
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for index, freq in postings:
                length_norm = 1.0 - self.b + self.b * (self._doc_lengths[index] / self.avg_doc_length)
                scores[index] += idf * (freq * (self.k1 + 1.0)) / (freq + self.k1 * length_norm)
        return scores
