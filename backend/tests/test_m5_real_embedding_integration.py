"""M5 closure — retrieval measured with the REAL configured embedding backend.

Two parts:

* Hermetic (always runs): the frozen dataset is intact, and a lexical-only
  baseline over it is computed — so the evaluation harness itself is tested.
* Integration (``-m integration``; skipped unless the real backend loads):
  the same frozen cases through lexical / dense / hybrid / hybrid+rerank using
  ``EmbeddingService()`` exactly as configured (no ConceptEmbedder, no mocks),
  reporting Recall@K, MRR and nDCG@K.

Running it for real:
    ATLAS_REAL_EMBEDDING_TESTS=1 pytest -m integration -s backend/tests/test_m5_real_embedding_integration.py
(the env var lets the suite's offline guard fetch the model weights once; with
weights already cached it also runs without it). Optionally set
ATLAS_RETRIEVAL_REPORT=/path/report.json to save the metrics.

Thresholds below are FLOORS chosen conservatively and overridable via env
(ATLAS_MIN_MRR / ATLAS_MIN_RECALL / ATLAS_MIN_NDCG). They have not been
calibrated against the real model in the authoring sandbox (weights could not be
downloaded there); calibrate them on the first real run.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.evaluation.retrieval_metrics import evaluate_rankings
from backend.retrieval import HybridRetriever, RetrievalDocument

DATA = Path(__file__).parent / "data" / "m5_retrieval_cases.json"
PINNED_SHA256 = "568b15cd263c26378ccc950c0bad3865d22e4397fcc4b350183758b195aae81a"
K = 3
MODES = {
    "lexical": dict(use_dense=False, rerank=False),
    "dense": dict(use_lexical=False, rerank=False),
    "hybrid": dict(rerank=False),
    "hybrid+rerank": dict(),
}


def _load():
    return json.loads(DATA.read_text(encoding="utf-8"))


def _documents(embeddings=None):
    corpus = _load()["corpus"]
    ids = list(corpus)
    return [
        RetrievalDocument(doc_id=i, text=corpus[i], embedding=(embeddings[n] if embeddings else None))
        for n, i in enumerate(ids)
    ]


def _run(retriever, documents, mode):
    out = {}
    last_diag = None
    for case in _load()["queries"]:
        res = retriever.retrieve(case["query"], documents, top_k=K, **MODES[mode])
        out[case["id"]] = (res.doc_ids(), set(case["relevant"]))
        last_diag = res.diagnostics
    return evaluate_rankings(out, k=K), out, last_diag


# ── hermetic ─────────────────────────────────────────────────────────────────
def test_frozen_cases_are_intact_and_well_formed():
    data = _load()
    canon = json.dumps({"corpus": data["corpus"], "queries": data["queries"]}, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canon.encode()).hexdigest() == PINNED_SHA256 == data["sha256"], (
        "Frozen retrieval cases were modified. Add a new versioned dataset instead of editing this one."
    )
    ids = set(data["corpus"])
    assert len(data["queries"]) >= 12 and len({q["id"] for q in data["queries"]}) == len(data["queries"])
    for q in data["queries"]:
        assert q["relevant"] and set(q["relevant"]) <= ids and q["query"].strip()


def test_lexical_baseline_on_frozen_cases_is_computed_hermetically():
    metrics, rankings, diag = _run(HybridRetriever(None), _documents(), "lexical")
    assert set(metrics) == {"recall@k", "precision@k", "mrr", "ndcg@k"} and all(0.0 <= v <= 1.0 for v in metrics.values())
    # exact-identifier queries are what lexical retrieval must get right
    for qid in ("q03", "q11"):
        assert rankings[qid][0][0] in rankings[qid][1], rankings[qid]
    # pure-paraphrase queries are where it should NOT be perfect — the set is discriminating
    assert metrics["mrr"] < 1.0 and diag.mode == "lexical_only"


# ── integration: real configured backend ─────────────────────────────────────
@pytest.fixture(scope="module")
def real_embedder():
    from backend.memory.embedding_service import EmbeddingService

    service = EmbeddingService()  # configured model, nothing monkeypatched
    if not service.is_available:
        pytest.skip(
            f"real embedding backend '{service.model_id}' is not available here "
            f"(mode={service.mode}); set ATLAS_REAL_EMBEDDING_TESTS=1 with network access, or cache the weights."
        )
    return service


@pytest.mark.integration
def test_real_embedding_backend_retrieval_quality_on_frozen_cases(real_embedder):
    texts = list(_load()["corpus"].values())
    vectors = real_embedder.embed_texts(texts)
    assert len(vectors) == len(texts) and len({len(v) for v in vectors}) == 1
    docs = _documents(vectors)
    retriever = HybridRetriever(real_embedder)

    results = {}
    for mode in MODES:
        metrics, _, diag = _run(retriever, docs, mode)
        results[mode] = metrics
        if mode in ("dense", "hybrid", "hybrid+rerank"):
            assert diag.dense_used, f"{mode}: real dense retrieval did not participate ({diag.dense_error})"

    report = {"model": real_embedder.model_id, "dimension": real_embedder.dimension, "k": K, "dataset_sha256": PINNED_SHA256, "metrics": results}
    print("\nREAL-EMBEDDING RETRIEVAL REPORT\n" + json.dumps(report, indent=1))
    if os.environ.get("ATLAS_RETRIEVAL_REPORT"):
        Path(os.environ["ATLAS_RETRIEVAL_REPORT"]).write_text(json.dumps(report, indent=1))

    best = results["hybrid+rerank"]
    assert best["mrr"] >= float(os.environ.get("ATLAS_MIN_MRR", 0.6)), results
    assert best["recall@k"] >= float(os.environ.get("ATLAS_MIN_RECALL", 0.75)), results
    assert best["ndcg@k"] >= float(os.environ.get("ATLAS_MIN_NDCG", 0.6)), results
    # Fusing in dense must not make things worse than BM25 alone (tiny tolerance).
    assert results["hybrid"]["mrr"] >= results["lexical"]["mrr"] - 1e-9, results
    assert results["hybrid"]["recall@k"] >= results["lexical"]["recall@k"] - 1e-9, results


@pytest.mark.integration
def test_real_embedding_vectors_are_deterministic_and_semantically_ordered(real_embedder):
    a, b, c = real_embedder.embed_texts(["a car drives down the road", "an automobile travels along the street", "boiling pasta in salted water"])
    from backend.retrieval.dense import cosine_similarity

    assert cosine_similarity(a, b) > cosine_similarity(a, c)
    assert real_embedder.embed_texts(["a car drives down the road"])[0] == pytest.approx(a, abs=1e-5)
