"""M7 — challenges: pure classification, pipeline through the real tool registry,
provenance, persistence/idempotency, failure/retry, isolation, Knowledge Map
integration (history preserved), and regressions."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.challenges import classify as C
from backend.challenges.classify import BaseEvidence, NewEvidence, classify, interpret
from backend.challenges.engine import ChallengeEngine, ChallengeToolFailure, ClaimContext
from backend.challenges.models import CHALLENGE_TYPES, MAX_ATTEMPTS, RESULTS, RUNNING_LEASE_SECONDS
from backend.challenges.queries import FOCUS, build_queries
from backend.challenges.repository import ChallengeConflict, ChallengeNotFound, ChallengeRepository
from backend.challenges.service import ChallengeService, ChallengeStateError
from backend.knowledge import ids
from backend.knowledge.map_service import KnowledgeMapService, NotFoundError
from backend.knowledge.repository import KnowledgeRepository
from backend.tests.m7_fixtures import (  # noqa: F401  (pytest fixtures + helpers)
    NOW, REPUTATION, S1, S2, S3, FakeWorld, _fake_embeddings, claim, cs, engine, evolving_scenario, investigate,
    make_user, repository, src, svc, world,
)

NEW_A, NEW_B, NEW_C = "https://news.example.io/a", "https://other.example.io/b", "https://third.example.io/c"
AS_OF = date(2026, 10, 4)
REP = lambda d: REPUTATION.get(d, 0.5)  # noqa: E731


@pytest.fixture()
def alice(repository):
    u = make_user(repository, "Alice")
    return u, evolving_scenario(repository, u)


def claim_ref(u, r, text):
    """(claim_id, investigation_id) — Tidal was only observed in R1, everything else in R2."""
    return ids.claim_id(u, text), (r["r1"] if text == "Tidal pilot launched" else r["r2"])


def challenge(cs, u, r, text, ctype):
    cid, inv = claim_ref(u, r, text)
    created, _ = cs.create(u, inv, cid, ctype)
    view, _ = cs.execute(u, created["challenge_id"])
    return view


# ══ pure classification ═══════════════════════════════════════════════════════
def ne(stance, strength=0.8, domain="x.io", independent=True, published=None, i=[0]):
    i[0] += 1
    return NewEvidence(f"src_{i[0]}", f"https://{domain}/{i[0]}", domain, stance, strength, independent, published)


def cls(ctype, new=(), base=(BaseEvidence("https://a.com/1", "a.com", "support", 0.8, date(2026, 8, 1)),), retrieved=None, scraped=None):
    new = list(new)
    return classify(ctype, base_evidence=list(base), new_evidence=new, retrieved_sources=len(new) if retrieved is None else retrieved,
                    scraped_sources=len(new) if scraped is None else scraped, as_of=AS_OF, reputation=REP)


@pytest.mark.parametrize("ctype", ["counter_evidence", "contradiction_search", "alternative_explanation", "assumption_check"])
def test_adversarial_types_share_the_core_policy(ctype):
    assert cls(ctype, [ne("contradict", 0.9)]).result == "contradicted"                          # strong, unopposed
    assert cls(ctype, [ne("contradict", 0.9), ne("support", 0.95)]).result == "weakened"          # strong but not outweighing? 0.9 vs .95
    assert cls(ctype, [ne("contradict", 0.4)]).result == "weakened"                               # weak contradiction
    assert cls(ctype, [ne("mixed", 0.7)]).result == "weakened"                                    # qualifying evidence
    assert cls(ctype, [ne("support", 0.7)]).result == "supported"
    assert cls(ctype, [ne("contradict", 0.2), ne("support", 0.9), ne("support", 0.8)]).result == "supported"  # survived
    assert cls(ctype, []).result == "unresolved"


def test_strong_contradiction_is_never_outweighed_into_supported():
    r = cls("counter_evidence", [ne("contradict", 0.7), ne("support", 0.9), ne("support", 0.9), ne("support", 0.9)])
    assert r.result == "weakened"  # c_w .7 < s_w 2.7 so not contradicted, but a strong contradiction blocks "survived"


def test_qualifying_weight_differs_by_type():
    just_mixed = [ne("mixed", 0.5), ne("support", 0.6)]
    assert cls("counter_evidence", just_mixed).result == "supported"        # 0.5*0.5=.25 adverse; 0.6 >= .5
    assert cls("assumption_check", just_mixed).result == "weakened"          # full weight: .5 adverse; .6 < 1.0


def test_unresolved_reasons_are_specific():
    assert "independent" in cls("counter_evidence", [], retrieved=0, scraped=0).rationale
    assert "none could be retrieved" in cls("counter_evidence", [], retrieved=3, scraped=0).rationale
    assert "no evidence that bears" in cls("counter_evidence", [], retrieved=3, scraped=3).rationale


def test_source_quality_outcomes():
    one = (BaseEvidence("https://a.com/1", "a.example.com", "support", 0.8),)
    two = one + (BaseEvidence("https://c.com/1", "c.example.net", "support", 0.8),)
    low = (BaseEvidence("https://b.com/1", "b.example.org", "support", 0.8), BaseEvidence("https://l.com/1", "lowrep.example.net", "support", 0.8))
    assert cls("source_quality", [], base=low).result == "source_concern"
    assert "single source" in cls("source_quality", [], base=one).rationale
    assert cls("source_quality", [], base=two).result == "unresolved"                                   # 2 domains, nothing new
    assert cls("source_quality", [ne("support", 0.7)], base=one).result == "supported"                  # independent corroboration
    assert cls("source_quality", [ne("support", 0.7, independent=False)], base=one).result == "source_concern"  # same-domain is not independent
    assert cls("source_quality", [ne("support", 0.7, domain="lowrep.example.net")], base=one).result == "source_concern"
    assert cls("source_quality", [ne("support", 0.7), ne("mixed", 0.9)], base=one).result == "weakened"
    assert cls("source_quality", [ne("contradict", 0.9)], base=two).result == "contradicted"


def test_temporal_validity_outcomes():
    old = (BaseEvidence("https://a.com/1", "a.com", "support", 0.8, date(2024, 1, 1)),)
    fresh = (BaseEvidence("https://a.com/1", "a.com", "support", 0.8, date(2026, 8, 1)),)
    undated = (BaseEvidence("https://a.com/1", "a.com", "support", 0.8, None),)
    newer = date(2026, 9, 1)
    assert cls("temporal_validity", [], base=old).result == "stale"
    assert cls("temporal_validity", [ne("support", 0.7, published=newer)], base=old).result == "supported"   # re-validated
    assert cls("temporal_validity", [ne("contradict", 0.9, published=newer)], base=old).result == "contradicted"
    assert cls("temporal_validity", [ne("contradict", 0.9, published=None)], base=old).result == "weakened"  # undated ≠ provably newer
    assert cls("temporal_validity", [ne("mixed", 0.5, published=newer)], base=fresh).result == "weakened"
    assert cls("temporal_validity", [ne("contradict", 0.9, published=date(2023, 1, 1))], base=old).result == "stale"  # OLDER contradiction is not "newer evidence"
    assert cls("temporal_validity", [], base=fresh).result == "supported"
    assert cls("temporal_validity", [], base=undated).result == "unresolved"


def test_every_result_value_is_reachable():
    produced = {
        cls("counter_evidence", [ne("support", 0.7)]).result, cls("counter_evidence", [ne("contradict", 0.4)]).result,
        cls("counter_evidence", [ne("contradict", 0.9)]).result, cls("counter_evidence", []).result,
        cls("temporal_validity", [], base=(BaseEvidence("u", "a.com", "support", 0.8, date(2024, 1, 1)),)).result,
        cls("source_quality", [], base=(BaseEvidence("u", "b.example.org", "support", 0.8),)).result,
    }
    assert produced == set(RESULTS)


def test_classification_is_deterministic_and_explains_itself():
    a = cls("counter_evidence", [ne("contradict", 0.4)])
    assert a.factors["contradicting"] == 1 and a.factors["challenge_type"] == "counter_evidence" and a.rationale
    assert (a.result, a.rationale, a.factors) == (cls("counter_evidence", [ne("contradict", 0.4)]).result, a.rationale, a.factors)


# ══ interpretation ═══════════════════════════════════════════════════════════
def ch(ctype, result, at="2026-09-01T00:00:00+00:00", status="completed", cid=None):
    return {"challenge_id": cid or f"chl_{ctype}_{result}", "challenge_type": ctype, "status": status, "result": result, "completed_at": at}


def test_interpretation_precedence_and_confidence():
    assert interpret("verified", 0.8, [])["label"] == "unchallenged"
    assert interpret("verified", 0.8, [ch("counter_evidence", "supported")])["label"] == "supported"
    assert interpret("verified", 0.8, [ch("counter_evidence", "supported")])["adjusted_confidence"] == 0.85
    mixed = interpret("verified", 0.8, [ch("counter_evidence", "supported"), ch("temporal_validity", "stale"), ch("source_quality", "weakened")])
    assert mixed["label"] == "weakened" and mixed["adjusted_confidence"] == 0.55 and len(mixed["basis"]) == 3
    assert interpret("verified", 0.8, [ch("counter_evidence", "contradicted"), ch("source_quality", "weakened")])["label"] == "contradicted"
    assert interpret("verified", 0.3, [ch("counter_evidence", "contradicted")])["adjusted_confidence"] == 0.0  # clamped
    assert interpret("verified", 0.8, [ch("counter_evidence", "unresolved")])["label"] == "unresolved"
    assert interpret("verified", 0.8, [ch("source_quality", "source_concern")])["label"] == "source_concern"


def test_interpretation_uses_only_completed_and_the_latest_per_type():
    rows = [ch("counter_evidence", "contradicted", "2026-01-01T00:00:00+00:00"), ch("counter_evidence", "supported", "2026-06-01T00:00:00+00:00"),
            ch("temporal_validity", None, status="failed"), ch("assumption_check", None, status="pending")]
    i = interpret("verified", 0.8, rows)
    assert i["label"] == "supported" and i["counts"]["contradicted"] == 0   # the newer re-challenge supersedes the older result
    assert i["basis"] == ["chl_counter_evidence_supported"]


# ══ queries ══════════════════════════════════════════════════════════════════
def test_queries_are_deterministic_bounded_and_type_specific():
    seen = set()
    for t in CHALLENGE_TYPES:
        q = build_queries(t, "Solar capacity grew in Europe", year=2026)
        assert q == build_queries(t, "Solar capacity grew in Europe", year=2026) and 1 <= len(q) <= 2
        assert all(x["question"] == "Solar capacity grew in Europe" and x["focus"] for x in q)
        seen.add(tuple(x["focus"] for x in q))
    assert len(seen) == len(CHALLENGE_TYPES) == len(FOCUS)
    assert "2026" in build_queries("temporal_validity", "x" * 20, year=2026)[0]["focus"]
    assert len(build_queries("counter_evidence", "word " * 400, year=2026)[0]["question"]) < 300
    with pytest.raises(ValueError):
        build_queries("free_form_agent", "claim text", year=2026)


# ══ pipeline through the real registry ═══════════════════════════════════════
@pytest.mark.parametrize("setup,ctype,text,expected", [
    (lambda w: w.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4), "counter_evidence", "Solar capacity grew in Europe", "contradicted"),
    (lambda w: w.add(NEW_A, "contradict", 0.4), "contradiction_search", "Solar capacity grew in Europe", "weakened"),
    (lambda w: w.add(NEW_A, "contradict", 0.2).add(NEW_B, "support", 0.9).add(NEW_C, "support", 0.8), "counter_evidence", "Solar capacity grew in Europe", "supported"),
    (lambda w: w.add(NEW_A, "support", 0.7), "counter_evidence", "Solar capacity grew in Europe", "supported"),
    (lambda w: w, "counter_evidence", "Solar capacity grew in Europe", "unresolved"),
    (lambda w: w.add(NEW_A), "counter_evidence", "Solar capacity grew in Europe", "unresolved"),                        # page has nothing relevant
    (lambda w: w.add(NEW_A, "mixed", 0.7), "assumption_check", "Solar capacity grew in Europe", "weakened"),
    (lambda w: w.add(NEW_A, "mixed", 0.7), "alternative_explanation", "Solar capacity grew in Europe", "weakened"),
    (lambda w: w, "source_quality", "Wind output doubled", "source_concern"),                                           # only a 0.2-reputation domain
    (lambda w: w, "source_quality", "Hydro is stable", "source_concern"),                                               # single source
    (lambda w: w.add(NEW_A, "support", 0.7), "source_quality", "Hydro is stable", "supported"),                          # independently corroborated
    (lambda w: w, "temporal_validity", "Tidal pilot launched", "stale"),
    (lambda w: w.add(NEW_A, "support", 0.7, published="2026-09-01"), "temporal_validity", "Tidal pilot launched", "supported"),
    (lambda w: w.add(NEW_A, "contradict", 0.9, published="2026-09-01"), "temporal_validity", "Tidal pilot launched", "contradicted"),
    (lambda w: w, "temporal_validity", "Solar capacity grew in Europe", "supported"),                                    # recent sources
])
def test_challenge_classifies_end_to_end(cs, world, alice, setup, ctype, text, expected):
    u, r = alice
    setup(world)
    view = challenge(cs, u, r, text, ctype)
    assert view["status"] == "completed" and view["result"] == expected, view["rationale"]
    assert view["rationale"]


def test_unreachable_sources_resolve_to_unresolved_not_supported(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.9)
    world.unreachable.add(NEW_A)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    assert view["result"] == "unresolved" and "none could be retrieved" in view["rationale"] and world.calls["verify"] == 0


def test_queries_sent_to_search_are_the_deterministic_ones(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.6)
    challenge(cs, u, r, "Solar capacity grew in Europe", "contradiction_search")
    assert len(world.queries) == 2 and all("Solar capacity grew in Europe" in q for q in world.queries)
    assert any("debunked" in q for q in world.queries) and any("conflicting findings" in q for q in world.queries)


# ══ independence + provenance ═══════════════════════════════════════════════
def test_original_sources_are_excluded_and_same_domain_is_not_independent(cs, world, alice):
    u, r = alice
    world.add(S1, "support", 0.9)                                    # the claim's own source
    world.add("https://a.example.com/other", "support", 0.7)         # same DOMAIN as an original source
    world.add(NEW_A, "support", 0.7)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    d = cs.inspect(u, view["challenge_id"])
    urls = {s["url"]: s for s in d["sources"]}
    assert S1 not in urls                                            # excluded: not independent evidence
    assert urls["https://a.example.com/other"]["independent"] is False and urls[NEW_A]["independent"] is True
    assert {e["source"]["url"]: e["source"]["independent"] for e in d["evidence"]}["https://a.example.com/other"] is False


def test_invented_evidence_urls_never_become_evidence(cs, world, alice, repository):
    u, r = alice
    world.add(NEW_A, "support", 0.7)
    world.invented = [("https://hallucinated.example.net/paper", "contradict", 0.99)]
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    d = cs.inspect(u, view["challenge_id"])
    assert d["dropped_unverified_evidence"] == 1
    assert [e["source"]["url"] for e in d["evidence"]] == [NEW_A]
    assert view["result"] == "supported"                              # the fake 0.99 contradiction must not influence the outcome
    assert "hallucinated" not in json.dumps(d)


def test_every_evidence_row_references_a_scraped_retrieved_source(cs, world, alice, repository):
    u, r = alice
    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.6)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    d = cs.inspect(u, view["challenge_id"])
    retrieved = {s["source_id"]: s for s in d["sources"]}
    assert d["evidence"] and all(e["source"]["source_id"] in retrieved and retrieved[e["source"]["source_id"]]["scraped"] for e in d["evidence"])
    assert all(e["provenance"] == {"retrieved_source": True} and e["source"]["content_hash"] and e["source"]["retrieved_at"] for e in d["evidence"])
    assert all(e["evidence_id"] == ids.evidence_id(u, e["source"]["source_id"], e["passage"]) for e in d["evidence"])  # stable, namespaced IDs
    assert ChallengeRepository(repository.db).integrity_problems() == []


def test_database_itself_rejects_evidence_without_a_retrieved_source(cs, world, alice, repository):
    u, r = alice
    world.add(NEW_A, "support", 0.6)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    with repository.db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO challenge_evidence (challenge_id,evidence_id,source_id,passage_text,stance,strength) VALUES (?,?,?,?,?,?)",
                         (view["challenge_id"], "evd_x", "src_never_retrieved", "p", "support", 0.5))
        with pytest.raises(sqlite3.IntegrityError):  # a 'completed' challenge must carry a result
            conn.execute("UPDATE challenges SET result = NULL WHERE challenge_id = ?", (view["challenge_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE challenges SET result = 'bogus' WHERE challenge_id = ?", (view["challenge_id"],))


def test_trace_is_recorded_safely(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.6)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    trace = cs.inspect(u, view["challenge_id"])["trace"]
    assert [t["action"] for t in trace] == ["plan", "search", "search", "scrape", "verify", "classify"]
    assert all(t["component"] == "challenge" and t["status"] in ("succeeded", "skipped") for t in trace)
    assert "news.example.io/a" not in json.dumps(trace)              # no URLs/content in the public trace


# ══ creation, persistence, idempotency ═══════════════════════════════════════
def test_create_validates_and_snapshots_the_base_claim(cs, alice):
    u, r = alice
    cid, inv = claim_ref(u, r, "Grid storage is cheap")
    view, created = cs.create(u, inv, cid, "counter_evidence")
    assert created and view["status"] == "pending" and view["result"] is None and view["challenge_id"].startswith("chl_")
    assert view["base"]["status"] == "conflicting" and view["base"]["confidence"] == 0.3 and view["claim_text"] == "Grid storage is cheap"
    with pytest.raises(ValueError):
        cs.create(u, inv, cid, "make_up_anything")
    with pytest.raises(ChallengeNotFound):
        cs.create(u, inv, "clm_" + "0" * 32, "counter_evidence")
    with pytest.raises(ChallengeNotFound):
        cs.create(u, "report_nope", cid, "counter_evidence")
    with pytest.raises(ChallengeNotFound):                             # claim exists but was not observed in that investigation
        cs.create(u, r["r2"], ids.claim_id(u, "Tidal pilot launched"), "counter_evidence")


def test_create_is_idempotent_while_active_and_history_accumulates_after(cs, world, alice):
    u, r = alice
    cid, inv = claim_ref(u, r, "Hydro is stable")
    a, c1 = cs.create(u, inv, cid, "counter_evidence")
    b, c2 = cs.create(u, inv, cid, "counter_evidence")
    assert (c1, c2) == (True, False) and a["challenge_id"] == b["challenge_id"]
    assert cs.create(u, inv, cid, "source_quality")[0]["challenge_id"] != a["challenge_id"]   # a different type is a different challenge
    world.add(NEW_A, "support", 0.7)
    cs.execute(u, a["challenge_id"])
    again, c3 = cs.create(u, inv, cid, "counter_evidence")                                    # completed → a fresh re-challenge
    assert c3 and again["challenge_id"] != a["challenge_id"]
    assert cs.list(u, claim_id=cid, challenge_type="counter_evidence")["total"] == 2          # both kept: history


def test_execute_is_idempotent_and_does_not_rerun(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "contradict", 0.9)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    calls = dict(world.calls)
    again, ran = cs.execute(u, view["challenge_id"])
    assert ran is False and again == view and world.calls == calls


def test_results_persist_across_service_restarts(repository, world, alice, engine):
    u, r = alice
    world.add(NEW_A, "contradict", 0.9)
    first = ChallengeService(repository, engine=engine, clock=lambda: NOW)
    view = challenge(first, u, r, "Solar capacity grew in Europe", "counter_evidence")
    fresh = ChallengeService(repository, engine=engine, clock=lambda: NOW)  # simulates a restart
    assert fresh.get(u, view["challenge_id"]) == view
    assert fresh.inspect(u, view["challenge_id"])["evidence"] == first.inspect(u, view["challenge_id"])["evidence"]


def test_at_most_one_active_challenge_per_tuple_is_enforced_by_the_database(cs, alice, repository):
    u, r = alice
    cid, inv = claim_ref(u, r, "Hydro is stable")
    cs.create(u, inv, cid, "counter_evidence")
    with repository.db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO challenges (challenge_id,user_id,investigation_id,claim_id,challenge_type,status,claim_text,created_at,updated_at) "
                         "VALUES ('chl_dup',?,?,?,?,'pending','x','t','t')", (u, inv, cid, "counter_evidence"))


def test_list_filters_and_pagination(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.7)
    for text, t in [("Hydro is stable", "counter_evidence"), ("Hydro is stable", "source_quality"), ("Solar capacity grew in Europe", "counter_evidence")]:
        challenge(cs, u, r, text, t)
    assert cs.list(u)["total"] == 3
    assert cs.list(u, claim_id=ids.claim_id(u, "Hydro is stable"))["total"] == 2
    assert cs.list(u, challenge_type="source_quality")["total"] == 1
    assert cs.list(u, status="completed")["total"] == 3 and cs.list(u, status="failed")["total"] == 0
    assert cs.list(u, investigation_id=r["r2"])["total"] == 3
    page = cs.list(u, limit=2, offset=1)
    assert len(page["items"]) == 2 and page["total"] == 3 and cs.list(u, limit=1000)["limit"] == 100


# ══ failure and retry ════════════════════════════════════════════════════════
def test_failed_challenge_is_recorded_not_completed_and_hides_the_raw_error(cs, world, alice):
    u, r = alice
    world.search_fail = 2
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    assert view["status"] == "failed" and view["result"] is None and view["retryable"] is True and view["attempt_count"] == 1
    assert view["error_type"] == "ChallengeToolFailure" and "search backend down" not in json.dumps(view)
    d = cs.inspect(u, view["challenge_id"])
    assert [(a["attempt_no"], a["status"]) for a in d["attempts"]] == [(1, "failed")] and d["evidence"] == [] and d["trace"]


def test_retry_after_failure_completes_and_keeps_attempt_history(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "contradict", 0.9)
    world.search_fail = 2
    failed = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    done, ran = cs.execute(u, failed["challenge_id"])
    assert ran and done["status"] == "completed" and done["result"] == "contradicted" and done["attempt_count"] == 2 and done["error_type"] is None
    assert [(a["attempt_no"], a["status"], a["error_type"]) for a in cs.inspect(u, done["challenge_id"])["attempts"]] == \
        [(1, "failed", "ChallengeToolFailure"), (2, "completed", None)]


@pytest.mark.parametrize("breaker", ["verify_fail", "scrape_fail"])
def test_other_stage_failures_fail_the_attempt(cs, world, alice, breaker):
    u, r = alice
    world.add(NEW_A, "support", 0.7)
    setattr(world, breaker, 1 if breaker == "verify_fail" else True)
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    assert view["status"] == "failed" and view["result"] is None


def test_one_failed_query_does_not_fail_the_challenge(cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.7)
    world.search_fail = 1
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    assert view["status"] == "completed" and view["result"] == "supported"


def test_retries_are_bounded(cs, world, alice):
    u, r = alice
    world.search_fail = 99
    view = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    for _ in range(MAX_ATTEMPTS - 1):
        view, _ = cs.execute(u, view["challenge_id"])
    assert view["status"] == "failed" and view["attempt_count"] == MAX_ATTEMPTS and view["retryable"] is False
    with pytest.raises(ChallengeConflict):
        cs.execute(u, view["challenge_id"])


def test_a_running_challenge_cannot_be_executed_twice_until_its_lease_expires(repository, alice, world):
    u, r = alice
    clock = {"t": NOW}
    engine = ChallengeEngine(world.registry(), clock=lambda: clock["t"], reputation=REP)
    svc_ = ChallengeService(repository, engine=engine, clock=lambda: clock["t"])
    cid, inv = claim_ref(u, r, "Hydro is stable")
    created, _ = svc_.create(u, inv, cid, "counter_evidence")
    svc_.repo.begin_attempt(u, created["challenge_id"], clock["t"])            # another worker holds it
    with pytest.raises(ChallengeConflict, match="already running"):
        svc_.execute(u, created["challenge_id"])
    clock["t"] = NOW + timedelta(seconds=RUNNING_LEASE_SECONDS + 5)             # lease expired: abandoned
    world.add(NEW_A, "support", 0.7)
    view, ran = svc_.execute(u, created["challenge_id"])
    assert ran and view["status"] == "completed" and view["attempt_count"] == 2


def test_a_stale_worker_cannot_overwrite_a_newer_attempt(repository, alice, world, cs):
    u, r = alice
    cid, inv = claim_ref(u, r, "Hydro is stable")
    created, _ = cs.create(u, inv, cid, "counter_evidence")
    _, attempt1 = cs.repo.begin_attempt(u, created["challenge_id"], NOW)
    _, attempt2 = cs.repo.begin_attempt(u, created["challenge_id"], NOW + timedelta(seconds=RUNNING_LEASE_SECONDS + 5))
    with pytest.raises(ChallengeConflict):
        cs.repo.complete(u, created["challenge_id"], attempt1, result="supported", result_json={}, sources=[], evidence=[], trace=[], now=NOW)
    assert cs.repo.fail(u, created["challenge_id"], attempt1, error_type="X", message="late", trace=[], now=NOW) is False
    assert cs.get(u, created["challenge_id"])["status"] == "running" and attempt2 == 2


def test_retrying_a_failed_challenge_conflicts_with_a_newer_active_one(cs, world, alice):
    u, r = alice
    world.search_fail = 2
    failed = challenge(cs, u, r, "Hydro is stable", "counter_evidence")
    cid, inv = claim_ref(u, r, "Hydro is stable")
    newer, created = cs.create(u, inv, cid, "counter_evidence")                 # allowed: the first is no longer active
    assert created and newer["challenge_id"] != failed["challenge_id"]
    with pytest.raises(ChallengeConflict, match="already active"):
        cs.execute(u, failed["challenge_id"])


def test_engine_failure_carries_the_partial_trace(world):
    world.search_fail = 5
    engine = ChallengeEngine(world.registry(), clock=lambda: NOW, reputation=REP)
    with pytest.raises(ChallengeToolFailure) as exc:
        engine.run(ClaimContext("chl_" + "1" * 32, "u1", "Solar capacity grew in Europe"), "counter_evidence")
    assert [t["action"] for t in exc.value.trace] == ["plan", "search", "search"] and all(t["status"] in ("succeeded", "failed") for t in exc.value.trace)


# ══ isolation ════════════════════════════════════════════════════════════════
def test_users_cannot_create_read_execute_or_list_each_others_challenges(repository, cs, world, alice):
    ua, ra = alice
    ub = make_user(repository, "Bob")
    rb = evolving_scenario(repository, ub)
    world.add(NEW_A, "contradict", 0.9)
    mine = challenge(cs, ua, ra, "Solar capacity grew in Europe", "counter_evidence")
    pending, _ = cs.create(ua, ra["r2"], ids.claim_id(ua, "Hydro is stable"), "counter_evidence")

    cid_a, inv_a = claim_ref(ua, ra, "Solar capacity grew in Europe")
    with pytest.raises(ChallengeNotFound):
        cs.create(ub, inv_a, cid_a, "counter_evidence")                          # Bob targets Alice's claim + investigation
    with pytest.raises(ChallengeNotFound):
        cs.create(ub, rb["r2"], cid_a, "counter_evidence")                        # Alice's claim in Bob's investigation
    calls = dict(world.calls)
    for fn in (cs.get, cs.inspect, cs.execute):
        with pytest.raises(ChallengeNotFound):
            fn(ub, pending["challenge_id"])
    assert world.calls == calls and cs.get(ua, pending["challenge_id"])["status"] == "pending"   # nothing ran, nothing changed
    assert cs.list(ub)["total"] == 0 and cs.list(ub, claim_id=cid_a)["items"] == []
    assert cs.list(ua)["total"] == 2
    # same claim text, same URLs, different owner → disjoint IDs and rows
    bob_view = challenge(cs, ub, rb, "Solar capacity grew in Europe", "counter_evidence")
    assert bob_view["challenge_id"] != mine["challenge_id"] and bob_view["claim_id"] != mine["claim_id"]
    assert {e["evidence_id"] for e in cs.inspect(ub, bob_view["challenge_id"])["evidence"]}.isdisjoint(
        {e["evidence_id"] for e in cs.inspect(ua, mine["challenge_id"])["evidence"]})
    assert cs.list(ua)["total"] == 2 and cs.list(ub)["total"] == 1


def test_repository_reads_are_user_scoped_even_when_called_directly(repository, cs, world, alice):
    ua, ra = alice
    ub = make_user(repository, "Bob")
    world.add(NEW_A, "contradict", 0.9)
    view = challenge(cs, ua, ra, "Solar capacity grew in Europe", "counter_evidence")
    repo = ChallengeRepository(repository.db)
    for read in (repo.get, repo.attempts, repo.sources, repo.evidence):
        try:
            assert not read(ub, view["challenge_id"])
        except ChallengeNotFound:
            pass
    assert repo.list_for_claim(ub, view["claim_id"]) == [] and repo.counts_by_result(ub) == {}


# ══ Knowledge Map integration: history preserved ═════════════════════════════
def snapshot_history(repository, u):
    with repository.db.get_connection() as conn:
        return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t} WHERE user_id = ?", (u,)).fetchall()))
                for t in ("knowledge_claims", "knowledge_evidence", "knowledge_sources", "knowledge_claim_observations", "knowledge_claim_evidence")}


def test_challenges_never_modify_existing_observations_or_evidence(repository, cs, world, svc, alice):
    u, r = alice
    before_rows, before_diff = snapshot_history(repository, u), svc.what_changed(u, r["r1"], r["r2"])
    before_claim = svc.get_claim(u, ids.claim_id(u, "Solar capacity grew in Europe"))
    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4)
    challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    challenge(cs, u, r, "Solar capacity grew in Europe", "temporal_validity")
    assert snapshot_history(repository, u) == before_rows                       # byte-for-byte: no M5 row touched
    assert svc.what_changed(u, r["r1"], r["r2"]) == before_diff
    after = svc.get_claim(u, ids.claim_id(u, "Solar capacity grew in Europe"))
    for key in ("status", "confidence", "history", "evidence", "observed_at", "last_verified_at"):
        assert after[key] == before_claim[key], key                              # original interpretation intact
    assert KnowledgeRepository(repository.db).integrity_problems() == []


def test_claim_view_shows_challenge_counter_evidence_result_and_updated_interpretation(repository, cs, world, svc, alice):
    u, r = alice
    claim_id = ids.claim_id(u, "Solar capacity grew in Europe")
    assert svc.get_claim(u, claim_id)["interpretation"]["label"] == "unchallenged" and svc.get_claim(u, claim_id)["challenge_trail"] is None
    world.add(NEW_A, "contradict", 0.9)
    done = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    d = svc.get_claim(u, claim_id)
    assert d["challenges"] == {"total": 1, "completed": 1, "failed": 0, "active": 0, "latest_result": "contradicted"}
    assert d["interpretation"]["label"] == "contradicted" and d["interpretation"]["original_status"] == "verified"
    assert d["interpretation"]["adjusted_confidence"] == 0.5 and d["confidence"] == 0.9    # adjusted is a view; the original stays 0.9
    [row] = d["challenges_detail"]
    assert row["challenge_id"] == done["challenge_id"] and row["result"] == "contradicted" and row["counter_evidence_count"] == 1
    assert [s["step"] for s in d["challenge_trail"]] == ["claim", "evidence", "challenge", "counter_evidence", "result", "updated_interpretation"]
    assert d["challenge_trail"][3]["items"][0]["url"] == NEW_A and d["challenge_trail"][4]["value"] == "contradicted"
    assert svc.list_claims(u, query="solar")["items"][0]["interpretation"]["label"] == "contradicted"
    assert svc.summary(u)["challenges"] == {"total": 1, "by_status": {"completed": 1}, "by_result": {"contradicted": 1}}


def test_failed_and_pending_challenges_are_visible_but_do_not_change_interpretation(cs, world, svc, alice):
    u, r = alice
    world.search_fail = 2
    challenge(cs, u, r, "Hydro is stable", "counter_evidence")                   # failed
    cid, inv = claim_ref(u, r, "Hydro is stable")
    cs.create(u, inv, cid, "source_quality")                                      # pending
    d = svc.get_claim(u, cid)
    assert d["challenges"] == {"total": 2, "completed": 0, "failed": 1, "active": 1, "latest_result": None}
    assert d["interpretation"]["label"] == "unchallenged" and d["challenge_trail"] is None
    assert {c["status"] for c in d["challenges_detail"]} == {"failed", "pending"}


def test_graph_links_claim_to_challenge_and_counter_evidence(cs, world, svc, alice):
    u, r = alice
    world.add(NEW_A, "contradict", 0.9).add(NEW_B, "support", 0.4)
    done = challenge(cs, u, r, "Solar capacity grew in Europe", "counter_evidence")
    claim_id = ids.claim_id(u, "Solar capacity grew in Europe")
    g = svc.graph(u, focus=claim_id, depth=2, limit=100)
    by_type = {}
    for n in g["nodes"]:
        by_type.setdefault(n["type"], []).append(n)
    assert {"claim", "challenge", "counter", "evidence", "source", "entity"} <= set(by_type)
    node_ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in node_ids and e["target"] in node_ids for e in g["edges"])        # never dangling
    types = {e["type"] for e in g["edges"]}
    assert {"challenged_by", "counter_evidence", "supporting_evidence"} <= types
    assert by_type["challenge"][0]["id"] == done["challenge_id"] and by_type["challenge"][0]["meta"]["result"] == "contradicted"
    assert {n["meta"]["stance"] for n in by_type["counter"]} == {"contradict", "support"}
    focused = svc.graph(u, focus=done["challenge_id"], depth=1)
    assert {n["type"] for n in focused["nodes"]} == {"challenge", "claim", "counter"}
    counter_id = by_type["counter"][0]["id"]
    assert {n["id"] for n in svc.graph(u, focus=counter_id, depth=1)["nodes"]} >= {counter_id, done["challenge_id"]}
    assert svc.graph(u, limit=300)["edges"] is not None  # overview still builds


def test_graph_challenge_nodes_are_isolated_per_user(repository, cs, world, svc, alice):
    ua, ra = alice
    ub = make_user(repository, "Bob")
    evolving_scenario(repository, ub)
    world.add(NEW_A, "contradict", 0.9)
    done = challenge(cs, ua, ra, "Solar capacity grew in Europe", "counter_evidence")
    with pytest.raises(NotFoundError):
        svc.graph(ub, focus=done["challenge_id"])
    assert all(n["type"] not in ("challenge", "counter") for n in svc.graph(ub, limit=300)["nodes"])
    assert svc.summary(ub)["challenges"]["total"] == 0


def test_challenging_a_failed_investigation_is_refused_but_partial_is_allowed(repository, cs, svc, alice):
    u, r = alice
    cid, inv = claim_ref(u, r, "Hydro is stable")
    KnowledgeRepository(repository.db).record_failure(u, inv, "boom")
    with pytest.raises(ChallengeStateError, match="failed"):
        cs.create(u, inv, cid, "counter_evidence")
    with repository.db.get_connection() as conn:                                   # simulate a successful rebuild of R2
        conn.execute("UPDATE knowledge_investigation_status SET status='complete', error=NULL WHERE report_id=?", (inv,))
        conn.commit()
    assert cs.create(u, inv, cid, "counter_evidence")[1] is True
    p = investigate(repository, u, "P", "2026-06-02T09:00:00+00:00", [src(S1, "2026-05-20")],
                    [claim("A grounded partial claim", evidence=[(S1, "Reports say A grounded partial claim.", "support", 0.8),
                                                                  ("https://invented.example.net/x", "Fake.", "support", 0.9)])])
    assert cs.create(u, p, ids.claim_id(u, "A grounded partial claim"), "counter_evidence")[0]["base"]["graph_state"] == "partial"


def test_challenge_survives_graph_rebuild_that_drops_the_claim_and_dies_with_its_report(repository, cs, world, svc, alice):
    from backend.knowledge.service import KnowledgeService

    u, r = alice
    world.add(NEW_A, "contradict", 0.9)
    done = challenge(cs, u, r, "Tidal pilot launched", "counter_evidence")                  # claim observed only in R1
    KnowledgeService(repository).record_investigation(u, r["r1"], [], [])                    # re-record R1 without the claim → claim GC'd
    with pytest.raises(NotFoundError):
        svc.get_claim(u, ids.claim_id(u, "Tidal pilot launched"))
    kept = cs.inspect(u, done["challenge_id"])                                              # history survives: soft reference + stored text
    assert kept["claim_text"] == "Tidal pilot launched" and kept["result"] == "contradicted" and kept["evidence"]
    assert repository.reports.delete_report(r["r1"]) is True                                 # deleting the investigation removes its challenges
    with pytest.raises(ChallengeNotFound):
        cs.get(u, done["challenge_id"])
    with repository.db.get_connection() as conn:
        assert all(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in ("challenges", "challenge_sources", "challenge_evidence", "challenge_attempts"))


# ══ fresh-database migration + regression ═══════════════════════════════════
M7_TABLES = {"challenges", "challenge_attempts", "challenge_sources", "challenge_evidence"}


def test_fresh_database_creates_every_table_with_valid_foreign_keys(repository):
    with repository.db.get_connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert M7_TABLES <= tables and len([t for t in tables if t.startswith("knowledge_")]) == 10
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    repository.initialize()
    repository.initialize()                                                          # idempotent


def test_upgrading_a_pre_m7_database_adds_challenge_tables_and_keeps_data(repository, cs, alice):
    u, r = alice
    with repository.db.get_connection() as conn:
        for t in ("challenge_evidence", "challenge_sources", "challenge_attempts", "challenges"):
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
    repository.initialize()
    with repository.db.get_connection() as conn:
        assert M7_TABLES <= {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2 and conn.execute("SELECT COUNT(*) FROM knowledge_claims").fetchone()[0] == 6
    cid, inv = claim_ref(u, r, "Hydro is stable")
    assert cs.create(u, inv, cid, "counter_evidence")[1] is True


def test_deleting_an_investigation_with_challenges_does_not_raise(repository, cs, world, alice):
    u, r = alice
    world.add(NEW_A, "support", 0.7)
    challenge(cs, u, r, "Hydro is stable", "counter_evidence")
    assert repository.reports.delete_report(r["r2"]) is True


def test_challenge_package_does_not_load_heavy_clients_on_import():
    import subprocess, sys
    code = ("import sys, backend.challenges, backend.challenges.classify, backend.challenges.queries, backend.challenges.repository\n"
            "bad=[m for m in ('chromadb','sentence_transformers','torch') if m in sys.modules]\nassert not bad, bad")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stderr


def test_production_wiring_builds_the_real_registry_and_reputation_without_network():
    """No fakes: the default engine must be able to reach every tool it depends on."""
    from backend.agentic.common import ToolName
    from backend.challenges.engine import _default_registry, _default_reputation

    registry = _default_registry()
    names = {n.value if hasattr(n, "value") else str(n) for n in registry.names()}
    for needed in (ToolName.REQUEST_MORE_EVIDENCE, ToolName.SCRAPE_SOURCE, ToolName.VERIFY_CLAIM):
        assert needed.value in names, needed
    reputation = _default_reputation()
    assert 0.0 <= reputation("nature.com") <= 1.0 and reputation("nature.com") > reputation("unknown-blog.example") == 0.5


def test_the_engine_can_only_call_the_three_registered_tools(world, alice, cs):
    """No unrestricted agent: the pipeline's external actions are a fixed, closed set."""
    u, r = alice
    seen: list[str] = []
    inner = world.registry()
    original = inner.execute
    inner.execute = lambda name, payload=None: (seen.append(getattr(name, "value", str(name))), original(name, payload))[1]
    world.add(NEW_A, "support", 0.7)
    cid, inv = claim_ref(u, r, "Solar capacity grew in Europe")
    svc2 = ChallengeService(cs.repository, engine=ChallengeEngine(inner, clock=lambda: NOW, reputation=REP), clock=lambda: NOW)
    created, _ = svc2.create(u, inv, cid, "counter_evidence")
    svc2.execute(u, created["challenge_id"])
    assert set(seen) == {"request_more_evidence", "scrape_source", "verify_claim"}
