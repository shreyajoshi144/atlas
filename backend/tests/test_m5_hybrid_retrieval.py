"""M5 — hybrid retrieval: components, fusion, filters, rerank, failure modes,
ranking-service integration, and measured retrieval quality.

Dense behaviour is tested through ``ConceptEmbedder``, a TEST DOUBLE that maps
synonyms to shared concept dimensions. It validates the fusion/rerank/failure
mechanics and the evaluation harness — it says nothing about the real
all-MiniLM-L6-v2 model's quality, which cannot be downloaded in hermetic CI.
"""
from __future__ import annotations

import hashlib

import pytest

from backend.evaluation.retrieval_metrics import evaluate_rankings, ndcg_at_k, reciprocal_rank
from backend.retrieval import (
    BM25Index, HybridConfig, HybridRetriever, InvalidFilterError, RetrievalDocument, matches_filter,
)
from backend.retrieval.dense import cosine_similarity
from backend.retrieval.fusion import reciprocal_rank_fusion
from backend.retrieval.text import lexical_terms, tokenize
from backend.services.ranking_service import RankingService, RetrievedSource

CONCEPTS = {
    "vehicle": {"car", "cars", "automobile", "automobiles", "vehicle"},
    "maker": {"maker", "makers", "manufacturer", "manufacturers", "producers"},
    "solar": {"solar", "photovoltaic", "pv"},
    "growth": {"growth", "expanded", "expansion", "grew", "rapidly"},
    "battery": {"battery", "batteries", "lithium-ion"},
    "recycle": {"recycling", "recycle", "reuse", "old"},
    "payment": {"payment", "payments", "checkout"},
    "pet": {"cat", "cats", "dog", "dogs", "pets"},
    "bread": {"bread", "sourdough"},
}
_DIM = len(CONCEPTS)
_HASH_DIMS = 64


class ConceptEmbedder:
    """Deterministic semantic-ish embedder (test double)."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * (_DIM + _HASH_DIMS)
            for tok in tokenize(text) + lexical_terms(text, remove_stopwords=True):
                for i, words in enumerate(CONCEPTS.values()):
                    if tok in words:
                        vec[i] += 1.0
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _HASH_DIMS
                vec[_DIM + h] += 0.15  # weak lexical leakage, as real models have
            out.append(vec)
        return out


class FailingEmbedder:
    def embed_texts(self, texts):
        raise RuntimeError("embedding backend down")


CORPUS = [
    RetrievalDocument("d1", "Automobile manufacturers are shifting toward electric propulsion", title="Industry"),
    RetrievalDocument("d2", "Error ERR-4417 raised by the payment gateway during checkout", title="Incident"),
    RetrievalDocument("d3", "Recipe for sourdough bread with long fermentation", title="Food"),
    RetrievalDocument("d4", "Photovoltaic installations expanded rapidly across Europe", title="Energy"),
    RetrievalDocument("d5", "Lithium-ion battery recycling plants open in Germany", title="Energy"),
    RetrievalDocument("d6", "Cats and dogs are common household pets", title="Pets"),
    RetrievalDocument("d7", "Solar eclipse viewing safety guidelines for children", title="Astronomy"),
    RetrievalDocument("d8", "Payment processing fees for small businesses", title="Finance"),
]
CASES = {
    "car makers going electric": {"d1"},        # needs semantics (car≈automobile)
    "ERR-4417 payment": {"d2"},                  # needs exact identifier match
    "solar panel growth in Europe": {"d4"},      # lexical distractor: d7 'solar'
    "recycling old batteries": {"d5"},           # plural/semantic
    "vehicle producers": {"d1"},                 # ZERO lexical overlap: BM25 alone must miss
}


def _run(mode: str):
    retriever = HybridRetriever(ConceptEmbedder())
    flags = {
        "lexical": dict(use_dense=False, rerank=False),
        "dense": dict(use_lexical=False, rerank=False),
        "hybrid": dict(rerank=False),
        "hybrid+rerank": dict(),
    }[mode]
    return {q: ([h.doc_id for h in retriever.retrieve(q, CORPUS, top_k=3, **flags).hits], rel) for q, rel in CASES.items()}


# ── components ───────────────────────────────────────────────────────────────
def test_bm25_has_real_idf_and_length_normalisation():
    docs = [lexical_terms(t) for t in ["battery energy density", "battery " * 6, "cats and dogs", "energy storage grid"]]
    idx = BM25Index(docs)
    assert idx.idf("density") > idx.idf("battery")  # rarer term weighs more
    short, long_ = idx.score(["battery"])[0], idx.score(["battery"])[1]
    assert short > 0 and long_ > 0
    # length normalisation is not a constant: score depends on doc length at equal tf
    a = BM25Index([["x", "y"], ["x"] + ["z"] * 20, ["q"]]).score(["x"])
    assert a[0] > a[1]


def test_identifier_tokens_stay_matchable():
    assert "err-4417" in lexical_terms("Error ERR-4417 raised")
    assert tokenize("Batteries") == tokenize("battery")


def test_cosine_distinguishes_missing_from_orthogonal():
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert cosine_similarity([1, 0], None) is None
    assert cosine_similarity([1, 0], [1, 0, 0]) is None
    assert cosine_similarity([0, 0], [1, 0]) is None


def test_rrf_rewards_agreement():
    fused = reciprocal_rank_fusion({"a": [1, 2, 3], "b": [2, 1, 4]}, k=60)
    assert fused[1] == pytest.approx(fused[2]) and fused[1] > fused[3] > fused[4] * 0.99


@pytest.mark.parametrize("meta,spec,expected", [
    ({"t": "x"}, {"t": "x"}, True),
    ({"t": "x"}, {"t": {"$in": ["a", "b"]}}, False),
    ({"d": "2025-03-01"}, {"d": {"$gte": "2025-01-01", "$lt": "2026-01-01"}}, True),
    ({}, {"d": {"$gte": "2025-01-01"}}, False),
    ({}, {"x": {"$ne": 1}}, True),
    ({"a": 1}, {"$or": [{"a": 2}, {"a": 1}]}, True),
    ({"s": 70}, {"s": {"$gte": 60}}, True),
])
def test_metadata_filters(meta, spec, expected):
    assert matches_filter(meta, spec) is expected


def test_invalid_filters_fail_loudly_not_silently():
    with pytest.raises(InvalidFilterError):
        matches_filter({}, {"x": {"$regex": "a"}})
    with pytest.raises(InvalidFilterError):
        HybridRetriever(None).retrieve("q", [], filters={"$nope": 1})  # even with no documents


# ── hybrid behaviour ─────────────────────────────────────────────────────────
def test_filters_are_applied_before_ranking():
    docs = [RetrievalDocument("a", "battery storage", metadata={"user": "u1"}),
            RetrievalDocument("b", "battery storage", metadata={"user": "u2"})]
    res = HybridRetriever(ConceptEmbedder()).retrieve("battery storage", docs, filters={"user": "u2"})
    assert res.doc_ids() == ["b"] and res.diagnostics.candidates_after_filter == 1


def test_dense_failure_degrades_to_lexical_and_is_reported():
    res = HybridRetriever(FailingEmbedder()).retrieve("battery recycling", CORPUS, top_k=2)
    assert res.diagnostics.mode == "lexical_only" and not res.diagnostics.dense_used
    assert "embedding backend down" in res.diagnostics.dense_error
    assert res.doc_ids()[0] == "d5"


def test_no_embedder_is_lexical_only():
    res = HybridRetriever(None).retrieve("sourdough bread", CORPUS, top_k=1)
    assert (res.diagnostics.mode, res.diagnostics.dense_error, res.doc_ids()) == ("lexical_only", "no_embedder", ["d3"])


def test_dimension_mismatched_vectors_are_excluded_from_dense_not_crashing():
    good = ConceptEmbedder().embed_texts(["battery recycling"])[0]
    docs = [RetrievalDocument("ok", "battery recycling plant", embedding=good),
            RetrievalDocument("old-model", "battery recycling plant", embedding=[0.1, 0.2, 0.3])]
    res = HybridRetriever(ConceptEmbedder()).retrieve("battery recycling", docs, embed_missing=False)
    assert res.diagnostics.missing_vectors == 1
    by_id = {h.doc_id: h for h in res.hits}
    assert by_id["ok"].dense_score is not None and by_id["old-model"].dense_score is None


def test_embed_missing_false_never_embeds_documents():
    emb = ConceptEmbedder()
    HybridRetriever(emb).retrieve("battery", CORPUS, embed_missing=False)
    assert emb.calls == 1  # only the query


def test_edge_inputs_return_empty():
    r = HybridRetriever(ConceptEmbedder())
    assert r.retrieve("   ", CORPUS).hits == [] and r.retrieve("x", []).hits == [] and r.retrieve("x", CORPUS, top_k=0).hits == []


def test_mmr_demotes_near_duplicates():
    dup = "battery recycling plants open in germany"
    docs = [RetrievalDocument("a", dup), RetrievalDocument("b", dup + " today"),
            RetrievalDocument("c", "battery recycling economics and policy in europe")]
    plain = HybridRetriever(None).retrieve("battery recycling", docs, top_k=3).doc_ids()
    diverse = HybridRetriever(None, config=HybridConfig(mmr_lambda=0.5)).retrieve("battery recycling", docs, top_k=3, diversify=True).doc_ids()
    assert diverse.index("c") <= plain.index("c")
    assert diverse.index("c") < diverse.index("b")


def test_reranker_boosts_exact_phrase():
    docs = [RetrievalDocument("scattered", "storage for energy with a density of batteries"),
            RetrievalDocument("phrase", "report on solid state battery energy density")]
    res = HybridRetriever(None).retrieve("battery energy density", docs, top_k=2)
    assert res.doc_ids()[0] == "phrase" and res.hits[0].rerank_score >= res.hits[1].rerank_score


# ── measured retrieval quality (ablation) ────────────────────────────────────
def test_hybrid_beats_each_single_retriever_on_labelled_cases():
    metrics = {m: evaluate_rankings(_run(m), k=3) for m in ("lexical", "dense", "hybrid", "hybrid+rerank")}
    for single in ("lexical", "dense"):
        assert metrics["hybrid"]["mrr"] >= metrics[single]["mrr"], metrics
        assert metrics["hybrid+rerank"]["mrr"] >= metrics[single]["mrr"], metrics
    assert metrics["hybrid+rerank"]["mrr"] == 1.0 and metrics["hybrid+rerank"]["recall@k"] == 1.0
    # The set is only meaningful if a single retriever genuinely misses something.
    # (This double leaks a little lexical signal, so dense does not fail on the
    # identifier case; no claim is made that real models handle identifiers well.)
    assert metrics["lexical"]["mrr"] < 1.0, metrics


def test_metric_functions():
    assert reciprocal_rank(["x", "a"], {"a"}) == 0.5
    assert ndcg_at_k(["a", "b"], {"a"}, 2) == 1.0 and ndcg_at_k(["b", "a"], {"a"}, 2) < 1.0


# ── RankingService integration ───────────────────────────────────────────────
def _sources():
    mk = lambda u, t, s: RetrievedSource(title=t, url=u, snippet=s, publish_date=None)
    return [
        mk("https://a.example.com/x", "Cats and dogs", "common household pets"),
        mk("https://b.example.com/y", "Photovoltaic installations", "expanded rapidly across Europe"),
        mk("https://c.example.com/z", "Solar eclipse safety", "viewing guidelines for children"),
    ]


def test_ranking_uses_dense_similarity_when_available():
    ranked, diag = RankingService(ConceptEmbedder()).rank_sources_with_diagnostics("solar panel growth in Europe", _sources())
    assert diag.dense_used and diag.mode == "hybrid"
    assert str(ranked[0].url).startswith("https://b.example.com") and ranked[0].cosine_score > 0
    assert ranked[0].bm25_score > 0  # real BM25, not the old constant-normalised score


def test_ranking_degrades_to_lexical_when_dense_fails_and_reports_it():
    ranked, diag = RankingService(FailingEmbedder()).rank_sources_with_diagnostics("solar panel growth in Europe", _sources())
    assert diag.mode == "lexical_only" and "embedding backend down" in diag.dense_error
    assert all(r.cosine_score == 0.0 for r in ranked) and ranked  # no fabricated cosine


def test_ranking_contract_is_preserved():
    ranked = RankingService(ConceptEmbedder()).rank_sources("solar", _sources())
    assert [r.rank_position for r in ranked] == [1, 2, 3]
    assert all(0 <= r.relevance_score <= 100 and 0 <= r.credibility_score <= 100 for r in ranked)
    assert RankingService(ConceptEmbedder()).rank_sources("", _sources()) == []
