"""M6 — 'What changed?' between two investigations."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.knowledge import ids
from backend.knowledge.map_service import ChangeComparisonError, KnowledgeMapService, NotFoundError
from backend.knowledge.repository import KnowledgeRepository
from backend.tests.m6_fixtures import _fake_embeddings, repository, svc  # noqa: F401  (pytest fixtures)
from backend.tests.m6_fixtures import NOW, S1, S2, S3, T1, T2, T3, claim, evolving_scenario, investigate, make_user, src


@pytest.fixture()
def world(repository):
    u = make_user(repository, "Alice")
    return u, evolving_scenario(repository, u)


def texts(section):
    return [c["text"] for c in section]


def test_every_change_class_is_detected(svc, world):
    u, r = world
    d = svc.what_changed(u, r["r1"], r["r2"])
    c = d["claims"]
    assert texts(c["new"]) == ["Offshore wind permits rose"]
    assert texts(c["strengthened"]) == ["Solar capacity grew in Europe"]
    assert texts(c["weakened"]) == ["Wind output doubled"]
    assert texts(c["contradicted"]) == ["Grid storage is cheap"]
    assert texts(c["unchanged"]) == ["Hydro is stable"]
    assert texts(c["not_reobserved"]) == ["Tidal pilot launched"]
    assert d["summary"] == {
        "new": 1, "strengthened": 1, "weakened": 1, "contradicted": 1, "unchanged": 1, "not_reobserved": 1,
        "stale": 2, "evidence_added": 2, "evidence_removed": 1, "evidence_stance_changed": 1,
        "evidence_stale": 2, "sources_changed": 2,
    }


def test_classes_are_mutually_exclusive_and_cover_every_claim(svc, world):
    u, r = world
    c = svc.what_changed(u, r["r1"], r["r2"])["claims"]
    ids_ = [x["claim_id"] for k, v in c.items() if k != "stale" for x in v]
    assert len(ids_) == len(set(ids_)) == 6


def test_entries_carry_before_after_and_reasons(svc, world):
    u, r = world
    c = svc.what_changed(u, r["r1"], r["r2"])["claims"]
    up = c["strengthened"][0]
    assert up["base"] == {"status": "partially_verified", "confidence": 0.5, "supporting": 1, "conflicting": 0}
    assert up["target"] == {"status": "verified", "confidence": 0.9, "supporting": 2, "conflicting": 0}
    assert up["score"] > 0.1 and any("confidence" in x for x in up["reasons"])
    assert c["weakened"][0]["score"] < -0.1
    assert c["new"][0]["base"] is None and c["not_reobserved"][0]["target"] is None
    assert "conflicting" in " ".join(c["contradicted"][0]["reasons"])


def test_evidence_level_changes(svc, world):
    u, r = world
    e = svc.what_changed(u, r["r1"], r["r2"])["evidence"]
    added = {(x["claim_id"], x["source"]["url"]): x for x in e["added"]}
    solar = ids.claim_id(u, "Solar capacity grew in Europe")
    assert (solar, S3) in added                                   # new supporting source on an existing claim
    assert any(x.get("claim_is_new") for x in e["added"])         # evidence of a brand-new claim
    assert any(x.get("claim_not_reobserved") for x in e["removed"])
    stance = e["stance_changed"]
    assert len(stance) == 1 and stance[0]["stance"] == "contradict" and stance[0]["previous_stance"] == "support"


def test_stale_claims_have_reasons_and_scope(svc, world):
    u, r = world
    stale = {s["text"]: s for s in svc.what_changed(u, r["r1"], r["r2"])["claims"]["stale"]}
    assert set(stale) == {"Tidal pilot launched", "Wind output doubled"}
    tidal = stale["Tidal pilot launched"]
    assert tidal["scope"] == "base_only"
    assert {x["code"] for x in tidal["stale"]["reasons"]} == {"age", "sources_old", "source_changed"}
    wind = stale["Wind output doubled"]
    assert wind["scope"] == "target" and [x["code"] for x in wind["stale"]["reasons"]] == ["sources_old"]


def test_stale_evidence_and_changed_sources(svc, world):
    u, r = world
    d = svc.what_changed(u, r["r1"], r["r2"])
    assert {x["source"]["url"] for x in d["evidence"]["stale"]} == {S2}
    changed = {s["url"]: s for s in d["sources"]["changed"]}
    assert set(changed) == {S1, S2} and changed[S1]["base_hash"] == "h1" and changed[S1]["target_hash"] == "h1b"
    wind = ids.claim_id(u, "Wind output doubled")
    assert wind in changed[S2]["affected_claim_ids"] and ids.claim_id(u, "Tidal pilot launched") in changed[S2]["affected_claim_ids"]


def test_result_is_deterministic_and_independent_of_the_wall_clock(repository, world):
    u, r = world
    a = KnowledgeMapService(repository, clock=lambda: NOW).what_changed(u, r["r1"], r["r2"])
    b = KnowledgeMapService(repository, clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)).what_changed(u, r["r1"], r["r2"])
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)  # staleness is as-of the TARGET investigation
    assert a["as_of"] == T2


def test_swapping_direction_inverts_strengthened_and_weakened(svc, world):
    u, r = world
    fwd, rev = svc.what_changed(u, r["r1"], r["r2"])["claims"], svc.what_changed(u, r["r2"], r["r1"])["claims"]
    # the reverse of a weakening is a strengthening — and so is the reverse of a contradiction (a recovery)
    assert texts(rev["weakened"]) == texts(fwd["strengthened"])
    assert sorted(texts(rev["strengthened"])) == sorted(texts(fwd["weakened"]) + texts(fwd["contradicted"]))
    assert rev["contradicted"] == []
    assert texts(rev["new"]) == texts(fwd["not_reobserved"]) and texts(rev["not_reobserved"]) == texts(fwd["new"])


def test_three_investigations_compare_pairwise_independently(repository, svc, world):
    u, r = world
    r3 = investigate(repository, u, "Energy", T3, [src(S1, "2026-05-20", hash="h1b"), src(S3, "2026-08-01", hash="h3")],
                     [claim("Hydro is stable", "verified", 0.95, [(S1, "Reports say Hydro is stable.", "support", 0.95)]),
                      claim("Solar capacity grew in Europe", "verified", 0.9, [(S1, "Reports say Solar capacity grew in Europe.", "support", 0.9)])])
    d = svc.what_changed(u, r["r2"], r3)
    assert texts(d["claims"]["strengthened"]) == ["Hydro is stable"]
    assert "Offshore wind permits rose" in texts(d["claims"]["not_reobserved"])
    assert svc.what_changed(u, r["r1"], r["r2"])["summary"]["new"] == 1  # earlier pair unaffected


def test_identical_investigations_report_no_change(repository, svc):
    u = make_user(repository, "Alice")
    spec = ([src(S1, "2026-05-20", hash="h1")], [claim("Stable fact", "verified", 0.8, [(S1, "Reports say Stable fact.", "support", 0.8)])])
    a = investigate(repository, u, "T", T1, *spec)
    b = investigate(repository, u, "T", T2, *spec)
    s = svc.what_changed(u, a, b)["summary"]
    assert (s["new"], s["strengthened"], s["weakened"], s["contradicted"], s["not_reobserved"], s["unchanged"]) == (0, 0, 0, 0, 0, 1)
    assert s["sources_changed"] == 0


# ── refusing bad input: failed / unrecorded / partial / invalid ──────────────
def test_failed_investigation_is_refused_not_compared_as_complete(svc, repository, world):
    u, r = world
    KnowledgeRepository(repository.db).record_failure(u, r["r2"], "boom")
    with pytest.raises(ChangeComparisonError) as exc:
        svc.what_changed(u, r["r1"], r["r2"])
    assert exc.value.state == "failed" and exc.value.report_id == r["r2"] and "rebuilt" in exc.value.reason
    with pytest.raises(ChangeComparisonError):
        svc.what_changed(u, r["r2"], r["r1"])  # in either position


def test_recovered_investigation_becomes_comparable_again(svc, repository, world):
    u, r = world
    KnowledgeRepository(repository.db).record_failure(u, r["r2"], "boom")
    svc.rebuild(u, r["r2"])
    assert svc.what_changed(u, r["r1"], r["r2"])["base"]["knowledge_state"] == "complete"


def test_not_recorded_investigation_is_refused(svc, repository, world):
    u, r = world
    with repository.db.get_connection() as conn:
        conn.execute("DELETE FROM knowledge_investigation_status WHERE report_id = ?", (r["r1"],))
        conn.commit()
    with pytest.raises(ChangeComparisonError) as exc:
        svc.what_changed(u, r["r1"], r["r2"])
    assert exc.value.state == "not_recorded"


def test_partial_investigation_compares_with_an_explicit_warning(svc, repository):
    u = make_user(repository, "Alice")
    ok = investigate(repository, u, "T", T1, [src(S1, "2026-05-20")], [claim("A claim", evidence=[(S1, "Reports say A claim.", "support", 0.8)])])
    part = investigate(repository, u, "T", T2, [src(S1, "2026-05-20")],
                       [claim("A claim", evidence=[(S1, "Reports say A claim.", "support", 0.8), ("https://invented.example.net/x", "Fake.", "support", 0.9)])])
    d = svc.what_changed(u, ok, part)
    assert d["target"]["knowledge_state"] == "partial" and d["base"]["knowledge_state"] == "complete"
    assert len(d["warnings"]) == 1 and part in d["warnings"][0] and "partial" in d["warnings"][0]


def test_invalid_comparisons(svc, world):
    u, r = world
    with pytest.raises(ValueError):
        svc.what_changed(u, r["r1"], r["r1"])
    with pytest.raises(NotFoundError):
        svc.what_changed(u, r["r1"], "report_doesnotexist")


def test_diff_only_uses_the_callers_data_even_when_texts_collide(svc, repository, world):
    ua, ra = world
    ub = make_user(repository, "Bob")
    rb = evolving_scenario(repository, ub)
    a, b = svc.what_changed(ua, ra["r1"], ra["r2"]), svc.what_changed(ub, rb["r1"], rb["r2"])
    assert a["summary"] == b["summary"]
    assert not ({x["claim_id"] for k in a["claims"] for x in a["claims"][k]} & {x["claim_id"] for k in b["claims"] for x in b["claims"][k]})
