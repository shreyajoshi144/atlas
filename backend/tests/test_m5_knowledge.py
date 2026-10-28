"""M5 — knowledge foundation: stable IDs, persisted claim↔evidence↔source↔entity
relationships, isolation, atomicity, orphan cleanup, migration/backfill, and the
hybrid report-memory path (isolation + failure modes)."""
from __future__ import annotations

import hashlib
import uuid

import pytest

from backend.knowledge import ids
from backend.knowledge.models import Evidence, InvestigationKnowledge, Source
from backend.knowledge.repository import KnowledgeRepository
from backend.knowledge.service import KnowledgeService, build_investigation_knowledge
from backend.models.domain_models import (
    ClaimEvidence, RankedSource, ResearchReport, ResearchSession, VerificationSummary, VerifiedClaim,
)


def _fake_vector(text: str, dim: int = 16) -> list[float]:
    return [b / 255.0 for b in hashlib.sha256(text.encode()).digest()[:dim]]


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    from backend.memory.embedding_service import EmbeddingService

    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, t: _fake_vector(t))
    monkeypatch.setattr(EmbeddingService, "embed_texts", lambda self, ts: [_fake_vector(t) for t in ts])


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    from backend.core import config as config_module
    from backend.database import ResearchRepository

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "atlas.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    config_module.get_settings.cache_clear()
    repo = ResearchRepository()
    repo.initialize()
    yield repo
    config_module.get_settings.cache_clear()


def _user(repository, name):
    from backend.auth.security import hash_password

    h, salt = hash_password("Sup3rSecret!")
    return repository.users.create(email=f"{name.lower()}@example.com", name=name, password_hash=h, password_salt=salt)["user_id"]


def _report(repository, user_id, topic="Batteries", text="body", sources=(), verification=None):
    topic_id = repository.topics.get_or_create_topic(topic, user_id=user_id)
    session = ResearchSession(topic=topic, status="completed")
    repository.sessions.save_session(topic_id, session, user_id=user_id)
    report = ResearchReport(report_id=f"report_{uuid.uuid4().hex[:12]}", topic=topic, summary="s",
                            report_markdown=text, key_statistics=[], swot={}, verification=verification)
    repository.reports.save_report(topic_id, session, report)
    if sources:
        repository.sources.replace_sources(report.report_id, session.session_id, list(sources))
    return topic_id, session, report


def _src(url, title="T"):
    return RankedSource(title=title, url=url, domain=url.split("/")[2], snippet="s", rank_position=1)


def _claim(text, urls, passage="Reports say Tesla expanded battery output in Germany.", stance="support"):
    return VerifiedClaim(
        claim_text=text, confidence=0.8, status="verified",
        evidence=[ClaimEvidence(source_id="", url=u, title="t", evidence_text=passage, stance=stance, evidence_strength=0.7) for u in urls],
    )


A, B = "https://a.example.com/post", "https://b.example.org/news"


# ── IDs ──────────────────────────────────────────────────────────────────────
def test_ids_are_stable_normalised_and_namespaced():
    assert ids.source_id("u1", "http://www.A.com/x/?utm_source=z#f") == ids.source_id("u1", "https://a.com/x")
    assert ids.claim_id("u1", " Solar GREW 20%. ") == ids.claim_id("u1", "solar grew 20%")
    assert ids.claim_id("u1", "x") != ids.claim_id("u2", "x")
    assert ids.source_id("u1", A).startswith("src_") and ids.entity_id("u1", "BYD").startswith("ent_")
    sid = ids.source_id("u1", A)
    assert ids.evidence_id("u1", sid, "Same passage.") == ids.evidence_id("u1", sid, "same passage")
    with pytest.raises(ValueError):
        ids.claim_id("", "x")
    with pytest.raises(ValueError):
        ids.canonicalize_url("not a url")


# ── persistence + relationships ──────────────────────────────────────────────
def test_relationships_are_persisted_and_traceable(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    stats = svc.record_investigation(u, rep.report_id, [_src(A), _src(B)], [_claim("Analysts say Tesla expanded output", [A, B])])
    assert stats["claims"] == 1 and stats["sources"] == 2 and stats["evidence"] == 2

    cid = ids.claim_id(u, "Analysts say Tesla expanded output")
    trace = svc.knowledge.get_claim_trace(u, cid)
    assert {e["source_id"] for e in trace["evidence"]} == {ids.source_id(u, A), ids.source_id(u, B)}
    assert all(e["stance"] == "support" and e["strength"] == 0.7 for e in trace["evidence"])
    assert trace["observations"] == [{"report_id": rep.report_id, "status": "verified", "confidence": 0.8}]
    names = {e["name"] for e in trace["entities"]}
    assert "Tesla" in names

    ent = svc.knowledge.get_entity_trace(u, ids.entity_id(u, "Tesla"))
    assert cid in {c["claim_id"] for c in ent["claims"]} and {s["url"] for s in ent["sources"]} == {A, B}
    assert svc.knowledge.integrity_problems() == []


def test_recording_is_idempotent(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    args = (u, rep.report_id, [_src(A)], [_claim("Claim one", [A])])
    svc.record_investigation(*args)
    first = svc.knowledge.count(u)
    svc.record_investigation(*args)
    assert svc.knowledge.count(u) == first and svc.knowledge.integrity_problems() == []


def test_hallucinated_evidence_url_never_becomes_knowledge(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    stats = svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Claim", [A, "https://invented.example.net/fake"])])
    assert stats["skipped_unresolved_evidence"] == 1 and stats["evidence"] == 1
    assert svc.knowledge.count(u)["sources"] == 1  # no source fabricated for the invented URL


def test_claim_without_evidence_is_kept_with_its_status(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [VerifiedClaim(claim_text="Unsupported claim")])
    row = svc.knowledge.list_report_claims(u, rep.report_id)[0]
    assert row["status"] == "insufficient_evidence" and svc.knowledge.integrity_problems() == []


def test_invalid_stance_is_normalised_and_duplicate_evidence_collapses():
    kn = build_investigation_knowledge("u1", [_src(A)], [
        _claim("C", [A, A], stance="weird")])
    assert len(kn.evidence) == 1 and len(kn.claim_evidence) == 1 and kn.claim_evidence[0].stance == "mixed"


# ── cross-investigation identity + orphan cleanup ────────────────────────────
def test_same_claim_across_reports_is_one_claim_and_cleanup_is_complete(repository):
    u = _user(repository, "Alice")
    _, _, r1 = _report(repository, u)
    _, _, r2 = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, r1.report_id, [_src(A)], [_claim("Shared claim", [A])])
    svc.record_investigation(u, r2.report_id, [_src(A)], [_claim("Shared claim", [A])])
    cid = ids.claim_id(u, "Shared claim")
    assert svc.knowledge.count(u)["claims"] == 1
    assert len(svc.knowledge.get_claim_trace(u, cid)["observations"]) == 2

    assert repository.reports.delete_report(r1.report_id)
    assert svc.knowledge.get_claim_trace(u, cid) is not None  # still observed by r2
    assert repository.reports.delete_report(r2.report_id)
    assert svc.knowledge.count(u) == {"sources": 0, "evidence": 0, "claims": 0, "entities": 0}
    assert svc.knowledge.integrity_problems() == []


def test_re_recording_a_report_drops_claims_no_longer_present(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Old claim", [A])])
    svc.record_investigation(u, rep.report_id, [_src(B)], [_claim("New claim", [B], passage="BYD grew in Europe.")])
    assert [c["claim_text"] for c in svc.knowledge.list_report_claims(u, rep.report_id)] == ["New claim"]
    assert svc.knowledge.get_claim_trace(u, ids.claim_id(u, "Old claim")) is None
    assert svc.knowledge.integrity_problems() == []


# ── isolation ────────────────────────────────────────────────────────────────
def test_knowledge_is_isolated_between_users(repository):
    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    _, _, ra = _report(repository, ua)
    _, _, rb = _report(repository, ub)
    svc = KnowledgeService(repository)
    svc.record_investigation(ua, ra.report_id, [_src(A)], [_claim("Same claim", [A])])
    svc.record_investigation(ub, rb.report_id, [_src(A)], [_claim("Same claim", [A])])

    ca, cb = ids.claim_id(ua, "Same claim"), ids.claim_id(ub, "Same claim")
    assert ca != cb and ids.source_id(ua, A) != ids.source_id(ub, A)
    assert svc.knowledge.get_claim_trace(ub, ca) is None and svc.knowledge.get_claim_trace(ua, cb) is None
    assert svc.knowledge.get_entity_trace(ub, ids.entity_id(ua, "Tesla")) is None
    assert svc.knowledge.list_report_claims(ub, ra.report_id) == []

    repository.reports.delete_report(ra.report_id)  # Alice's delete leaves Bob untouched
    assert svc.knowledge.get_claim_trace(ub, cb) is not None and svc.knowledge.count(ub)["claims"] == 1
    assert svc.knowledge.integrity_problems() == []


def test_cannot_record_into_another_users_report(repository):
    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    _, _, ra = _report(repository, ua)
    with pytest.raises(PermissionError):
        KnowledgeService(repository).record_investigation(ub, ra.report_id, [_src(A)], [_claim("x y", [A])])
    assert KnowledgeService(repository).knowledge.count(ub)["claims"] == 0


# ── atomicity ────────────────────────────────────────────────────────────────
def test_failed_write_rolls_back_and_keeps_previous_graph(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Good claim", [A])])
    before = svc.knowledge.count(u)

    bad = InvestigationKnowledge(
        sources=[Source(source_id=ids.source_id(u, B), canonical_url=ids.canonicalize_url(B), url=B, domain="b")],
        evidence=[Evidence(evidence_id="evd_bad", source_id="src_does_not_exist", passage_text="x")],  # FK violation
    )
    with pytest.raises(Exception):
        KnowledgeRepository(repository.db).replace_investigation(u, rep.report_id, bad)
    assert svc.knowledge.count(u) == before
    assert svc.knowledge.get_claim_trace(u, ids.claim_id(u, "Good claim")) is not None
    assert svc.knowledge.integrity_problems() == []


# ── M4 regression: deleting an evaluated report ──────────────────────────────
def test_delete_report_with_evaluation_row_succeeds(repository):
    from backend.evaluation.models import EvaluationSummary

    u = _user(repository, "Alice")
    topic_id, session, rep = _report(repository, u)
    repository.evaluations.save_evaluation(
        rep.report_id, session.session_id, topic_id,
        EvaluationSummary(report_id=rep.report_id, session_id=session.session_id, overall_score=0.5))
    assert repository.reports.delete_report(rep.report_id) is True  # raised IntegrityError before M5


# ── migration / backfill ─────────────────────────────────────────────────────
def test_migration_adds_knowledge_tables_to_an_m4_shaped_db_without_data_loss(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u, sources=[_src(A)])
    tables = [r[0] for r in repository.db.get_connection().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'knowledge_%'")]
    assert len(tables) == 10  # 9 graph tables + investigation status
    with repository.db.get_connection() as conn:  # simulate a pre-M5 database
        for t in reversed(tables):  # children before parents (FKs are enforced)
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
    repository.initialize()
    repository.initialize()  # idempotent
    assert repository.reports.get_report(report_id=rep.report_id, user_id=u)
    assert len(repository.sources.get_sources_by_report_id(rep.report_id)) == 1
    assert KnowledgeService(repository).knowledge.count(u)["claims"] == 0  # tables usable again


def test_backfill_rebuilds_graph_for_a_pre_m5_report(repository):
    u = _user(repository, "Alice")
    verification = VerificationSummary(overall_verification_score=80, verdict="ok", claims=[_claim("Backfilled claim", [A])])
    _, _, rep = _report(repository, u, sources=[_src(A)], verification=verification)
    svc = KnowledgeService(repository)
    assert svc.knowledge.list_report_claims(u, rep.report_id) == []
    stats = svc.backfill_report(u, rep.report_id)
    assert stats["claims"] == 1 and stats["evidence"] == 1
    assert svc.backfill_report(_user(repository, "Bob"), rep.report_id) is None  # not Bob's report
    assert svc.knowledge.integrity_problems() == []


# ── hybrid report memory ─────────────────────────────────────────────────────
def _indexed(repository, user_id, topic, text):
    from backend.memory import VectorMemoryService

    tid, session, rep = _report(repository, user_id, topic, text)
    vm = VectorMemoryService(repository=repository)
    vm.index_report(topic_id=tid, session=session, report=rep, user_id=user_id)
    return vm, rep


def test_hybrid_memory_search_is_user_scoped(repository):
    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    vm, ra = _indexed(repository, ua, "Secret topic", "Zyxwvut quantum widget prototype results are confidential.")
    assert [r["report_id"] for r in vm.search_similar("zyxwvut quantum widget", ua)] == [ra.report_id]
    assert vm.search_similar("zyxwvut quantum widget", ub) == []
    assert vm.retrieve_chat_context("Secret topic", "zyxwvut widget?", [], ub)["retrieved_chunks"] == []
    assert vm.retrieve_chat_context("Secret topic", "zyxwvut widget?", [], ua)["retrieved_chunks"]


def test_memory_search_still_works_lexically_when_vectors_are_missing(repository):
    ua = _user(repository, "Alice")
    vm, ra = _indexed(repository, ua, "Topic", "Perovskite tandem cells reached record efficiency.")
    vm.collection.delete(ids=[c["chunk_id"] for c in repository.chunks.get_chunks_by_report_id(ra.report_id)])
    res = vm.hybrid_search("perovskite efficiency", ua)
    assert res.hits and res.diagnostics.mode == "lexical_only" and res.diagnostics.missing_vectors == 1  # index drift is reported


def test_memory_filters_by_topic_and_report(repository):
    ua = _user(repository, "Alice")
    vm, r1 = _indexed(repository, ua, "Energy", "Grid storage with sodium batteries is growing.")
    _, r2 = _indexed(repository, ua, "Cooking", "Sodium content in bread recipes matters for health.")
    assert {h.document.metadata["report_id"] for h in vm.hybrid_search("sodium", ua).hits} == {r1.report_id, r2.report_id}
    assert [h.document.metadata["report_id"] for h in vm.hybrid_search("sodium", ua, topic="Cooking").hits] == [r2.report_id]
    only = vm.hybrid_search("sodium", ua, filters={"report_id": {"$in": [r1.report_id]}})
    assert [h.document.metadata["report_id"] for h in only.hits] == [r1.report_id]


def test_deleting_report_memory_removes_search_hits_for_owner_only(repository):
    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    vm, ra = _indexed(repository, ua, "Topic", "Unique flibbertigibbet research content here.")
    vm.delete_report_memory(ra.report_id, ub)
    assert vm.search_similar("flibbertigibbet", ua)
    vm.delete_report_memory(ra.report_id, ua)
    assert vm.search_similar("flibbertigibbet", ua) == []


def test_indexed_chunks_are_stamped_with_the_embedding_model(repository):
    ua = _user(repository, "Alice")
    vm, ra = _indexed(repository, ua, "Topic", "Some indexed content about wind turbines.")
    meta = vm.collection.get(where={"user_id": ua}, include=["metadatas"])["metadatas"][0]
    assert meta["embedding_model"]


# ── route-level: the research pipeline populates the graph ───────────────────
from backend.tests.test_milestone1_e2e import agentic_graph  # noqa: E402,F401  (reuse the fake-pipeline fixture)


def test_research_route_persists_knowledge_graph_and_stays_isolated(monkeypatch, repository, agentic_graph):
    import asyncio
    from types import SimpleNamespace

    from backend.api.routes import research as research_route
    from backend.evaluation.models import EvaluationSummary
    from backend.models.api_models import ResearchRunRequest

    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    monkeypatch.setattr(research_route, "_repository", lambda: repository)
    monkeypatch.setattr(research_route, "_vector_memory", lambda: SimpleNamespace(index_report=lambda **_: None))
    monkeypatch.setattr(
        research_route, "run_pipeline_evaluation",
        lambda **kw: EvaluationSummary(report_id=kw["report_id"], session_id=kw["session_id"], topic=kw["topic"], overall_score=0.9),
    )
    response = asyncio.run(research_route.run_research(
        ResearchRunRequest(topic="Atlas knowledge route", top_k_urls=1, persist_memory=True), {"user_id": ua}))
    report_id = response.data.report.report_id

    kr = KnowledgeRepository(repository.db)
    assert kr.count(ua)["sources"] >= 1, "ranked sources must be recorded as canonical Sources"
    assert kr.count(ub) == {"sources": 0, "evidence": 0, "claims": 0, "entities": 0}
    assert kr.integrity_problems() == []

    assert repository.reports.delete_report(report_id)  # evaluated report: also exercises the FK fix
    assert kr.count(ua) == {"sources": 0, "evidence": 0, "claims": 0, "entities": 0}


# ── explicit persistence status: complete / partial / failed ─────────────────
def _bad_bundle(u):
    """Passes validation but violates an FK at write time."""
    return InvestigationKnowledge(
        sources=[Source(source_id=ids.source_id(u, B), canonical_url=ids.canonicalize_url(B), url=B, domain="b")],
        evidence=[Evidence(evidence_id="evd_bad", source_id="src_does_not_exist", passage_text="x")],
    )


def test_status_is_not_recorded_until_a_write_is_attempted(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    assert svc.get_status(u, rep.report_id)["status"] == "not_recorded"
    assert svc.knowledge.is_complete(u, rep.report_id) is False


def test_clean_write_is_complete(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    stats = svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Clean claim", [A])])
    st = svc.get_status(u, rep.report_id)
    assert stats["status"] == st["status"] == "complete" and svc.knowledge.is_complete(u, rep.report_id)
    assert (st["dropped_sources"], st["dropped_evidence"], st["dropped_claims"], st["error"]) == (0, 0, 0, None)


def test_dropped_inputs_make_the_graph_partial_not_complete(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    # one invented evidence URL, one unusable source URL, one empty claim
    claims = [_claim("Real claim", [A, "https://invented.example.net/x"]), {"claim_text": "   ", "evidence": []}]
    stats = svc.record_investigation(u, rep.report_id, [_src(A), {"url": "not-a-url", "title": "x", "domain": "x"}], claims)
    st = svc.get_status(u, rep.report_id)
    assert stats["status"] == st["status"] == "partial"
    assert (st["dropped_evidence"], st["dropped_sources"], st["dropped_claims"]) == (1, 1, 1)
    assert svc.knowledge.is_complete(u, rep.report_id) is False
    assert svc.knowledge.count(u)["claims"] == 1  # what could be persisted still was


def test_failed_write_is_marked_failed_never_complete_and_keeps_stale_graph_flagged(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Good claim", [A])])
    assert svc.get_status(u, rep.report_id)["status"] == "complete"

    with pytest.raises(Exception):
        svc.knowledge.replace_investigation(u, rep.report_id, _bad_bundle(u))
    st = svc.get_status(u, rep.report_id)
    assert st["status"] == "failed" and st["error"] and "IntegrityError" in st["error"]
    assert svc.knowledge.is_complete(u, rep.report_id) is False   # stale graph must not read as complete
    assert svc.knowledge.get_claim_trace(u, ids.claim_id(u, "Good claim")) is not None  # rollback preserved it
    assert svc.knowledge.integrity_problems() == []


def test_first_ever_write_failing_leaves_no_graph_and_status_failed(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    with pytest.raises(Exception):
        svc.knowledge.replace_investigation(u, rep.report_id, _bad_bundle(u))
    assert svc.get_status(u, rep.report_id)["status"] == "failed"
    assert svc.knowledge.count(u) == {"sources": 0, "evidence": 0, "claims": 0, "entities": 0}


def test_successful_retry_after_failure_returns_to_complete_and_clears_error(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    with pytest.raises(Exception):
        svc.knowledge.replace_investigation(u, rep.report_id, _bad_bundle(u))
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Retry claim", [A])])
    st = svc.get_status(u, rep.report_id)
    assert st["status"] == "complete" and st["error"] is None


def test_build_failure_before_any_write_is_also_recorded_as_failed(repository):
    class ExplodingExtractor:
        def extract(self, text):
            raise RuntimeError("extractor crashed")

    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Earlier claim", [A])])
    bad = KnowledgeService(repository, extractor=ExplodingExtractor())
    with pytest.raises(RuntimeError, match="extractor crashed"):
        bad.record_investigation(u, rep.report_id, [_src(A)], [_claim("Later claim", [A])])
    st = svc.get_status(u, rep.report_id)
    assert st["status"] == "failed" and "extractor crashed" in st["error"]


def test_status_is_user_scoped_and_foreign_writes_leave_no_trace(repository):
    ua, ub = _user(repository, "Alice"), _user(repository, "Bob")
    _, _, ra = _report(repository, ua)
    svc = KnowledgeService(repository)
    svc.record_investigation(ua, ra.report_id, [_src(A)], [_claim("Alice claim", [A])])

    assert svc.get_status(ub, ra.report_id)["status"] == "not_recorded"       # Bob can't read Alice's status
    with pytest.raises(PermissionError):
        svc.record_investigation(ub, ra.report_id, [_src(A)], [_claim("Bob claim", [A])])
    assert svc.knowledge.record_failure(ub, ra.report_id, "forged") is False   # nor mark her report failed
    assert svc.get_status(ua, ra.report_id)["status"] == "complete"


def test_deleting_a_report_removes_its_status(repository):
    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    svc = KnowledgeService(repository)
    svc.record_investigation(u, rep.report_id, [_src(A)], [_claim("Doomed claim", [A])])
    assert repository.reports.delete_report(rep.report_id)
    assert svc.get_status(u, rep.report_id)["status"] == "not_recorded"
    with repository.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_investigation_status").fetchone()[0] == 0


def test_status_column_rejects_values_outside_the_three_states(repository):
    import sqlite3

    u = _user(repository, "Alice")
    _, _, rep = _report(repository, u)
    with repository.db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_investigation_status (report_id,user_id,status,updated_at) VALUES (?,?,?,?)",
                (rep.report_id, u, "done", "now"))


def test_route_knowledge_failure_stays_non_fatal_but_is_recorded_as_failed(monkeypatch, repository, agentic_graph):
    import asyncio
    from types import SimpleNamespace

    import backend.knowledge.service as knowledge_service
    from backend.api.routes import research as research_route
    from backend.evaluation.models import EvaluationSummary
    from backend.models.api_models import ResearchRunRequest

    u = _user(repository, "Alice")
    monkeypatch.setattr(research_route, "_repository", lambda: repository)
    monkeypatch.setattr(research_route, "_vector_memory", lambda: SimpleNamespace(index_report=lambda **_: None))
    monkeypatch.setattr(
        research_route, "run_pipeline_evaluation",
        lambda **kw: EvaluationSummary(report_id=kw["report_id"], session_id=kw["session_id"], topic=kw["topic"], overall_score=0.9),
    )
    monkeypatch.setattr(knowledge_service, "build_investigation_knowledge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph build exploded")))

    response = asyncio.run(research_route.run_research(
        ResearchRunRequest(topic="Atlas status route", top_k_urls=1, persist_memory=True), {"user_id": u}))
    assert response.data is not None and response.data.report.report_id   # research itself succeeded
    st = KnowledgeService(repository).get_status(u, response.data.report.report_id)
    assert st["status"] == "failed" and "graph build exploded" in st["error"]
    assert KnowledgeRepository(repository.db).count(u)["claims"] == 0


def test_heuristic_extractor_does_not_promote_standalone_month_to_entity():
    from backend.knowledge.extraction import HeuristicEntityExtractor

    extracted = HeuristicEntityExtractor().extract(
        "Revenue increased in February. CAGR remained a useful metric for Acme Energy."
    )
    names = {name.casefold() for name, _ in extracted}
    assert "february" not in names
    # Acronyms/concepts remain available; avoid over-filtering legitimate terms.
    assert "CAGR" in {name for name, kind in extracted if kind == "acronym"}
    assert "Acme Energy" in {name for name, kind in extracted if kind == "proper_noun"}


def test_month_filter_does_not_remove_month_inside_multiword_name():
    from backend.knowledge.extraction import HeuristicEntityExtractor

    extracted = HeuristicEntityExtractor().extract("February Capital announced a new fund.")
    assert ("February Capital", "proper_noun") in extracted
