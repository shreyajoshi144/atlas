"""M6 — Knowledge Map service: drill-down, claim/source freshness, graph integrity,
explicit knowledge states, authorisation/isolation, idempotency, regressions."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from backend.knowledge import ids
from backend.knowledge.map_service import KnowledgeMapService, NotFoundError
from backend.knowledge.repository import KnowledgeRepository
from backend.models.domain_models import RankedSource, ResearchReport, ResearchSession, VerificationSummary, VerifiedClaim, ClaimEvidence
from backend.tests.m6_fixtures import _fake_embeddings, repository, svc  # noqa: F401  (pytest fixtures)
from backend.tests.m6_fixtures import NOW, S1, S2, S3, T1, T2, claim, evolving_scenario, investigate, make_user, src


def cid(user, text):
    return ids.claim_id(user, text)


@pytest.fixture()
def alice(repository):
    u = make_user(repository, "Alice")
    return u, evolving_scenario(repository, u)


# ── drill-down: Entity → Claim → Evidence → Source ───────────────────────────
def test_drill_entity_to_claim_to_evidence_to_source_is_consistent(svc, alice):
    u, _ = alice
    entity = svc.get_entity(u, ids.entity_id(u, "Europe"))
    target = next(c for c in entity["claims"] if c["text"] == "Solar capacity grew in Europe")
    detail = svc.get_claim(u, target["claim_id"])
    assert {e["source"]["url"] for e in detail["evidence"]["supporting"]} == {S1, S3}

    first = detail["evidence"]["supporting"][0]
    ev = svc.get_evidence(u, first["evidence_id"])
    assert target["claim_id"] in {c["claim_id"] for c in ev["claims"]}
    assert ev["source"]["source_id"] == first["source"]["source_id"]

    source = svc.get_source(u, ev["source"]["source_id"])
    assert first["evidence_id"] in {e["evidence_id"] for e in source["evidence"]}
    assert target["claim_id"] in {c for e in source["evidence"] for c in e["claim_ids"]}


def test_entity_lists_search_sort_and_paginate(svc, alice):
    u, _ = alice
    everything = svc.list_entities(u, limit=100)
    assert everything["total"] >= 5 and everything["items"] == sorted(
        everything["items"], key=lambda i: (-i["claim_count"], i["name"].lower(), i["entity_id"]))
    hit = svc.list_entities(u, query="  EUROPE ")
    assert [i["name"] for i in hit["items"]] == ["Europe"]
    page = svc.list_entities(u, limit=2, offset=1)
    assert len(page["items"]) == 2 and page["total"] == everything["total"] and page["offset"] == 1
    assert svc.list_entities(u, limit=10_000)["limit"] == 100  # clamped, never unbounded
    assert svc.list_entities(u, query="zzz-no-such-entity")["items"] == []


# ── claim detail: confidence, status, evidence, freshness, last verified ─────
def test_claim_detail_shows_status_confidence_and_conflicting_evidence(svc, alice):
    u, ids_ = alice
    d = svc.get_claim(u, cid(u, "Grid storage is cheap"))
    assert (d["status"], d["confidence"]) == ("conflicting", 0.3)
    assert d["evidence"]["supporting"] == [] and [e["source"]["url"] for e in d["evidence"]["conflicting"]] == [S3]
    assert d["evidence_counts"] == {"supporting": 0, "conflicting": 1, "mixed": 0}
    # last verified is the LAST time it was verified/partially verified: R1, not the conflicting R2
    assert d["last_verified_at"] == T1 and d["observed_at"] == T2 and d["first_seen_at"] == T1
    assert [(h["report_id"], h["status"]) for h in d["history"]] == [(ids_["r1"], "verified"), (ids_["r2"], "conflicting")]


def test_last_verified_is_most_recent_verified_observation(svc, alice):
    u, _ = alice
    assert svc.get_claim(u, cid(u, "Hydro is stable"))["last_verified_at"] == T2
    assert svc.get_claim(u, cid(u, "Wind output doubled"))["last_verified_at"] == T2  # partially_verified counts


def test_source_freshness_labels_and_dates(svc, alice):
    u, _ = alice
    by_url = {s["url"]: s for s in svc.list_sources(u)["items"]}
    assert by_url[S3]["freshness"]["label"] == "fresh" and by_url[S1]["freshness"]["label"] == "aging"
    assert by_url[S2]["freshness"]["label"] == "old" and by_url[S2]["publish_date"] == "2024-01-01"
    assert [s["url"] for s in svc.list_sources(u, freshness="old")["items"]] == [S2]
    assert by_url[S1]["content_changed"] and by_url[S2]["content_changed"] and not by_url[S3]["content_changed"]


def test_claim_staleness_follows_the_clock(repository, alice):
    u, _ = alice
    soon = KnowledgeMapService(repository, clock=lambda: datetime(2026, 6, 20, tzinfo=timezone.utc))
    later = KnowledgeMapService(repository, clock=lambda: NOW)
    hydro = cid(u, "Hydro is stable")
    assert soon.get_claim(u, hydro)["stale"] == {"is_stale": False, "reasons": []}
    stale = later.get_claim(u, hydro)["stale"]
    assert stale["is_stale"] and [r["code"] for r in stale["reasons"]] == ["age"]
    assert {c["claim_id"] for c in later.list_claims(u, stale=True, limit=100)["items"]} >= {hydro}
    assert hydro not in {c["claim_id"] for c in soon.list_claims(u, stale=True, limit=100)["items"]}


def test_changed_source_marks_older_snapshot_stale_but_not_the_current_one(svc, alice):
    u, ids_ = alice
    wind = cid(u, "Wind output doubled")
    as_of_r1 = svc.get_claim(u, wind, report_id=ids_["r1"])
    support = as_of_r1["evidence"]["supporting"][0]["source"]
    assert support["changed_since_verified"] is True  # S2 re-fetched in R2 with a different hash
    assert "source_changed" in [r["code"] for r in as_of_r1["stale"]["reasons"]]
    current = svc.get_claim(u, wind)["evidence"]["supporting"][0]["source"]
    assert current["changed_since_verified"] is False


def test_claim_snapshots_by_investigation(svc, alice):
    u, ids_ = alice
    wind = cid(u, "Wind output doubled")
    r1, r2 = svc.get_claim(u, wind, report_id=ids_["r1"]), svc.get_claim(u, wind, report_id=ids_["r2"])
    assert (r1["status"], r1["confidence"]) == ("verified", 0.9) and (r2["status"], r2["confidence"]) == ("partially_verified", 0.4)
    with pytest.raises(NotFoundError):  # observed in R1 only
        svc.get_claim(u, cid(u, "Tidal pilot launched"), report_id=ids_["r2"])


def test_claim_list_filters(svc, alice):
    u, ids_ = alice
    texts = lambda **kw: {c["text"] for c in svc.list_claims(u, limit=100, **kw)["items"]}
    assert texts(status="conflicting") == {"Grid storage is cheap"}
    assert texts(conflicting=True) == {"Grid storage is cheap"}
    assert "Offshore wind permits rose" not in texts(report_id=ids_["r1"]) and "Tidal pilot launched" in texts(report_id=ids_["r1"])
    assert texts(query="solar") == {"Solar capacity grew in Europe"}
    assert texts(entity_id=ids.entity_id(u, "Europe")) == {"Solar capacity grew in Europe"}
    assert texts(status="conflicting", query="solar") == set()


# ── summary + explicit knowledge states ──────────────────────────────────────
def test_summary_counts_and_complete_map(svc, alice):
    u, _ = alice
    s = svc.summary(u)
    assert s["counts"]["claims"] == 6 and s["counts"]["sources"] == 3
    assert s["investigations"] == {"total": 2, "failed": 0, "not_recorded": 0, "partial": 0, "complete": 2}
    assert s["map_is_complete"] is True and s["integrity"] == {"ok": True, "problem_count": 0}
    assert s["conflicting_claims"] == 1 and s["stale_claims"] >= 1


def test_empty_workspace_is_not_reported_complete(svc, repository):
    u = make_user(repository, "Nobody")
    s = svc.summary(u)
    assert s["map_is_complete"] is False and s["counts"] == {"entities": 0, "claims": 0, "evidence": 0, "sources": 0}
    assert svc.graph(u)["nodes"] == [] and svc.list_claims(u)["items"] == []


def test_failed_persistence_is_visible_and_never_complete(svc, repository, alice):
    u, ids_ = alice
    KnowledgeRepository(repository.db).record_failure(u, ids_["r2"], RuntimeError("secret internal: table x"))

    inv = {i["report_id"]: i for i in svc.list_investigations(u)}
    assert inv[ids_["r2"]]["knowledge_state"] == "failed" and inv[ids_["r2"]]["is_complete"] is False
    assert inv[ids_["r2"]]["comparable"] is False and inv[ids_["r1"]]["knowledge_state"] == "complete"
    assert inv[ids_["r2"]]["error_type"] == "RuntimeError"
    assert "secret internal" not in json.dumps(svc.list_investigations(u))  # message never leaves the server

    s = svc.summary(u)
    assert s["map_is_complete"] is False and s["investigations"]["failed"] == 1

    # claims whose latest snapshot is the failed investigation are flagged, not trusted
    hydro = svc.get_claim(u, cid(u, "Hydro is stable"))
    assert hydro["graph_state"] == "failed" and hydro["is_complete"] is False
    assert svc.get_claim(u, cid(u, "Tidal pilot launched"))["graph_state"] == "complete"  # only R1 observed it
    assert svc.get_entity(u, ids.entity_id(u, "Europe"))["graph_state"] == "failed"
    assert {n["graph_state"] for n in svc.graph(u)["nodes"]} >= {"failed"}
    assert all(c["graph_state"] == "failed" for c in svc.list_claims(u, report_id=ids_["r2"], limit=100)["items"])


def test_partial_persistence_is_visible_as_partial(svc, repository):
    u = make_user(repository, "Alice")
    rid = investigate(repository, u, "T", T1, [src(S1, "2026-05-20")],
                      [claim("A grounded claim", evidence=[(S1, "Reports say A grounded claim.", "support", 0.8),
                                                         ("https://invented.example.net/x", "Invented.", "support", 0.9)])])
    inv = svc.list_investigations(u)[0]
    assert inv["knowledge_state"] == "partial" and inv["is_complete"] is False and inv["comparable"] is True
    assert inv["dropped"]["evidence"] == 1
    assert svc.get_claim(u, cid(u, "A grounded claim"))["graph_state"] == "partial"
    assert svc.summary(u)["map_is_complete"] is False


def test_unrecorded_report_is_listed_as_not_recorded_and_rebuild_recovers_it(svc, repository):
    u = make_user(repository, "Alice")
    verification = VerificationSummary(overall_verification_score=80, verdict="ok", claims=[VerifiedClaim(
        claim_text="Legacy claim", confidence=0.7, status="verified",
        evidence=[ClaimEvidence(source_id="", url=S1, title="t", evidence_text="Reports say Legacy claim.", stance="support", evidence_strength=0.7)])])
    topic_id = repository.topics.get_or_create_topic("Legacy", user_id=u)
    session = ResearchSession(topic="Legacy", status="completed")
    repository.sessions.save_session(topic_id, session, user_id=u)
    report = ResearchReport(report_id=f"report_{uuid.uuid4().hex[:12]}", topic="Legacy", summary="s", report_markdown="b",
                            key_statistics=[], swot={}, verification=verification)
    repository.reports.save_report(topic_id, session, report)
    repository.sources.replace_sources(report.report_id, session.session_id,
                                       [RankedSource(title="t", url=S1, domain="a.example.com", snippet="s", rank_position=1)])

    assert svc.list_investigations(u)[0]["knowledge_state"] == "not_recorded"
    assert svc.summary(u)["map_is_complete"] is False
    out = svc.rebuild(u, report.report_id)
    assert out["status"] == "complete" and svc.list_investigations(u)[0]["knowledge_state"] == "complete"
    assert svc.get_claim(u, cid(u, "Legacy claim"))["graph_state"] == "complete"


def test_rebuild_recovers_a_failed_state_and_is_idempotent(svc, repository, alice):
    u, ids_ = alice
    KnowledgeRepository(repository.db).record_failure(u, ids_["r2"], "boom")
    svc.rebuild(u, ids_["r2"])
    after_first = svc.summary(u)
    svc.rebuild(u, ids_["r2"])
    assert svc.summary(u) == after_first
    assert after_first["investigations"]["failed"] == 0
    assert {i["error_type"] for i in svc.list_investigations(u)} == {None}


# ── graph integrity ──────────────────────────────────────────────────────────
def _assert_graph_consistent(svc, u, g):
    node_ids = [n["id"] for n in g["nodes"]]
    assert len(node_ids) == len(set(node_ids)), "duplicate nodes"
    for e in g["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids, f"dangling edge {e}"
    getters = {"entity": svc.get_entity, "claim": svc.get_claim, "evidence": svc.get_evidence, "source": svc.get_source}
    for n in g["nodes"]:
        assert svc.node_type(n["id"]) == n["type"]
        getters[n["type"]](u, n["id"])  # every node resolves for its owner


def test_overview_graph_is_consistent_and_covers_all_four_layers(svc, alice):
    u, _ = alice
    g = svc.graph(u, limit=300)
    _assert_graph_consistent(svc, u, g)
    assert {n["type"] for n in g["nodes"]} == {"entity", "claim", "evidence", "source"}
    assert {e["type"] for e in g["edges"]} >= {"mentions", "supported_by", "contradicted_by", "from_source"}


def test_focused_graph_walks_entity_to_source_and_respects_depth(svc, alice):
    u, _ = alice
    focus = ids.entity_id(u, "Europe")
    d1, d3 = svc.graph(u, focus=focus, depth=1), svc.graph(u, focus=focus, depth=3, limit=300)
    assert {n["type"] for n in d1["nodes"]} <= {"entity", "claim", "evidence"} and "source" not in {n["type"] for n in d1["nodes"]}
    assert {"entity", "claim", "evidence", "source"} == {n["type"] for n in d3["nodes"]}
    for g in (d1, d3):
        _assert_graph_consistent(svc, u, g)


def test_truncated_graph_never_has_dangling_edges(svc, alice):
    u, _ = alice
    g = svc.graph(u, limit=5)
    assert g["truncated"] is True and len(g["nodes"]) == 5
    _assert_graph_consistent(svc, u, g)


def test_graph_reflects_contradicting_evidence_edge(svc, alice):
    u, _ = alice
    focus = cid(u, "Grid storage is cheap")
    g = svc.graph(u, focus=focus, depth=1)
    assert [e["type"] for e in g["edges"] if e["source"] == focus and e["type"] != "mentions"] == ["contradicted_by"]
    node = next(n for n in g["nodes"] if n["id"] == focus)
    assert node["meta"]["status"] == "conflicting" and node["meta"]["conflicting"] == 1


def test_graph_focus_must_exist(svc, alice):
    u, _ = alice
    with pytest.raises(NotFoundError):
        svc.graph(u, focus="clm_" + "0" * 32)


# ── authorisation / isolation ────────────────────────────────────────────────
def test_users_never_see_each_others_knowledge(svc, repository, alice):
    ua, ids_a = alice
    ub = make_user(repository, "Bob")
    ids_b = evolving_scenario(repository, ub)  # same URLs and claim text, different owner

    assert cid(ua, "Hydro is stable") != cid(ub, "Hydro is stable")
    a_claims = {c["claim_id"] for c in svc.list_claims(ua, limit=100)["items"]}
    b_claims = {c["claim_id"] for c in svc.list_claims(ub, limit=100)["items"]}
    assert a_claims and b_claims and not (a_claims & b_claims)
    assert {i["report_id"] for i in svc.list_investigations(ub)} == set(ids_b.values())

    for call in (
        lambda: svc.get_claim(ub, cid(ua, "Hydro is stable")),
        lambda: svc.get_entity(ub, ids.entity_id(ua, "Europe")),
        lambda: svc.get_source(ub, ids.source_id(ua, S1)),
        lambda: svc.get_evidence(ub, next(iter(svc.get_claim(ua, cid(ua, "Hydro is stable"))["evidence"]["supporting"]))["evidence_id"]),
        lambda: svc.graph(ub, focus=cid(ua, "Hydro is stable")),
        lambda: svc.list_claims(ub, report_id=ids_a["r1"]),
        lambda: svc.list_claims(ub, entity_id=ids.entity_id(ua, "Europe")),
        lambda: svc.what_changed(ub, ids_a["r1"], ids_a["r2"]),
        lambda: svc.what_changed(ub, ids_a["r1"], ids_b["r2"]),
        lambda: svc.rebuild(ub, ids_a["r1"]),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert svc.summary(ub)["counts"] == svc.summary(ua)["counts"]  # same shape, disjoint rows


def test_another_users_failed_state_does_not_leak_into_my_summary(svc, repository, alice):
    ua, ids_a = alice
    ub = make_user(repository, "Bob")
    evolving_scenario(repository, ub)
    KnowledgeRepository(repository.db).record_failure(ua, ids_a["r2"], "boom")
    assert svc.summary(ua)["map_is_complete"] is False and svc.summary(ub)["map_is_complete"] is True
    assert svc.summary(ub)["investigations"]["failed"] == 0


def test_cross_user_foreign_key_attack_via_rebuild_writes_nothing(svc, repository, alice):
    ua, ids_a = alice
    ub = make_user(repository, "Bob")
    before = svc.summary(ua)
    with pytest.raises(NotFoundError):
        svc.rebuild(ub, ids_a["r1"])
    assert svc.summary(ua) == before and svc.summary(ub)["counts"]["claims"] == 0


# ── idempotency / determinism ────────────────────────────────────────────────
def test_every_read_is_deterministic_for_a_fixed_clock(svc, alice):
    u, ids_ = alice
    c = cid(u, "Solar capacity grew in Europe")
    calls = [
        lambda: svc.summary(u), lambda: svc.list_investigations(u), lambda: svc.list_entities(u, limit=100),
        lambda: svc.list_claims(u, limit=100), lambda: svc.get_claim(u, c), lambda: svc.list_sources(u, limit=100),
        lambda: svc.graph(u, limit=300), lambda: svc.what_changed(u, ids_["r1"], ids_["r2"]),
    ]
    for call in calls:
        assert json.dumps(call(), sort_keys=True) == json.dumps(call(), sort_keys=True)


def test_re_recording_an_investigation_does_not_change_the_map(svc, repository, alice):
    from backend.knowledge.service import KnowledgeService

    u, ids_ = alice
    before = (svc.summary(u), svc.what_changed(u, ids_["r1"], ids_["r2"]))
    KnowledgeService(repository).backfill_report(u, ids_["r2"])  # re-derive from the stored report
    after = (svc.summary(u), svc.what_changed(u, ids_["r1"], ids_["r2"]))
    assert after[0]["counts"]["sources"] == before[0]["counts"]["sources"] and after[0]["investigations"]["complete"] >= 1
    assert KnowledgeRepository(repository.db).integrity_problems(user_id=u) == []


# ── regression: scoped integrity check ───────────────────────────────────────
def test_scoped_integrity_check_only_reports_the_callers_rows(repository, alice):
    ua, _ = alice
    ub = make_user(repository, "Bob")
    with repository.db.get_connection() as conn:  # plant an orphan claim for Bob only
        conn.execute("INSERT INTO knowledge_claims (claim_id,user_id,claim_text,normalized_text,created_at) VALUES (?,?,?,?,?)",
                     ("clm_" + "f" * 32, ub, "orphan", "orphan", "now"))
        conn.commit()
    kr = KnowledgeRepository(repository.db)
    assert kr.integrity_problems(user_id=ua) == []
    assert any("orphan claim" in p for p in kr.integrity_problems(user_id=ub))
    assert any("orphan claim" in p for p in kr.integrity_problems())
