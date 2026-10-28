"""Tokenisation shared by every lexical component of hybrid retrieval.

Two views of a text are exposed on purpose:

* :func:`tokenize` — ordered word tokens (used for phrase/bigram adjacency).
* :func:`lexical_terms` — the bag of terms BM25 indexes: the word tokens plus
  *compound identifiers* such as ``err-4417`` or ``v2.3.1`` kept whole, so an
  exact identifier query is matchable even though its parts are common.

Both apply the same conservative normalisation to queries and documents, so
matching is consistent regardless of how a stem looks to a human.
"""
from __future__ import annotations

import re

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing down
    during each few for from further had has have having he her here hers him his
    how i if in into is it its itself just me more most my no nor not of off on
    once only or other our out over own same she should so some such than that the
    their them then there these they this those through to too under until up very
    was we were what when where which while who whom why will with would you your
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_COMPOUND_RE = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)+")


def light_stem(token: str) -> str:
    """Very conservative plural folding (``batteries``→``battery``).

    It is intentionally not a linguistic stemmer: the only requirement is that
    queries and documents are folded identically.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str | None, *, remove_stopwords: bool = False) -> list[str]:
    """Ordered, stemmed word tokens. Stopwords are kept unless asked otherwise."""
    if not text:
        return []
    tokens = [light_stem(t) for t in _WORD_RE.findall(text.lower())]
    if remove_stopwords:
        kept = [t for t in tokens if t not in STOPWORDS]
        # A query made only of stopwords ("to be or not to be") must still match.
        return kept or tokens
    return tokens


def lexical_terms(text: str | None, *, remove_stopwords: bool = True) -> list[str]:
    """Terms BM25 indexes: stemmed words plus whole compound identifiers."""
    if not text:
        return []
    lowered = text.lower()
    terms = tokenize(lowered, remove_stopwords=remove_stopwords)
    terms.extend(m.group(0) for m in _COMPOUND_RE.finditer(lowered))
    return terms
