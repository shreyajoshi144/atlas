"""'What changed?' between two investigations (knowledge snapshots).

A snapshot is one investigation's persisted contribution to the graph: the
claims it observed (status, confidence) and the evidence links it recorded.
The comparison is a pure function of the two snapshots plus the persisted source
history, evaluated *as of the target investigation's time* — never the wall
clock — so repeating it always yields the same answer.

Claim classes (mutually exclusive): ``new``, ``strengthened``, ``weakened``,
``contradicted``, ``unchanged`` (in both), and ``not_reobserved`` (only in the
base). ``stale`` is a separate, overlapping flag list: claims (in the target, or
base-only) that should be re-checked as of the target time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.knowledge.freshness import ClaimSnapshot, classify_change, source_freshness

if TYPE_CHECKING:  # pragma: no cover
    from backend.knowledge.map_graph import UserGraph
    from backend.knowledge.map_service import KnowledgeMapService

_PASSAGE_MAX = 300


def build_snapshot(g: "UserGraph", report_id: str) -> dict[str, ClaimSnapshot]:
    out: dict[str, ClaimSnapshot] = {}
    for cid, observations in g.obs_by_claim.items():
        obs = next((o for o in observations if o.report_id == report_id), None)
        if obs is None:
            continue
        links = [l for l in g.links_by_claim_report.get((cid, report_id), []) if l.evidence_id in g.evidence]
        evidence = tuple(sorted(
            (l.evidence_id, l.stance, l.strength, g.evidence[l.evidence_id]["source_id"]) for l in links
        ))
        out[cid] = ClaimSnapshot(
            claim_id=cid, status=obs.status, confidence=obs.confidence,
            support=sum(1 for l in links if l.stance == "support"),
            conflict=sum(1 for l in links if l.stance == "contradict"),
            evidence=evidence,
        )
    return out


def _sig(snap: ClaimSnapshot | None) -> dict | None:
    if snap is None:
        return None
    return {"status": snap.status, "confidence": snap.confidence, "supporting": snap.support, "conflicting": snap.conflict}


def _score(base: ClaimSnapshot, target: ClaimSnapshot) -> float:
    return round((target.confidence - base.confidence) + 0.25 * (target.status_rank - base.status_rank)
                 + 0.05 * (target.net_support - base.net_support), 4)


def compare_investigations(
    service: "KnowledgeMapService", g: "UserGraph", base_id: str, target_id: str, *, warnings: list[str],
) -> dict[str, Any]:
    base_info, target_info = g.reports[base_id], g.reports[target_id]
    as_of = target_info.created_at
    base, target = build_snapshot(g, base_id), build_snapshot(g, target_id)

    def claim_entry(cid: str, **extra: Any) -> dict:
        return {"claim_id": cid, "text": g.claims[cid]["claim_text"], "base": _sig(base.get(cid)), "target": _sig(target.get(cid)), **extra}

    def ev_entry(evidence_id: str, claim_id: str, stance: str, strength: float, source_id: str, **extra: Any) -> dict:
        ev, src = g.evidence[evidence_id], g.sources.get(source_id, {})
        passage = ev["passage_text"]
        return {
            "evidence_id": evidence_id, "claim_id": claim_id, "stance": stance, "strength": strength,
            "passage": passage if len(passage) <= _PASSAGE_MAX else passage[: _PASSAGE_MAX - 1] + "…",
            "source": {"source_id": source_id, "url": src.get("url"), "domain": src.get("domain"),
                       "freshness": source_freshness(src.get("publish_date"), as_of=as_of)["label"]},
            **extra,
        }

    classes: dict[str, list[dict]] = {k: [] for k in ("new", "strengthened", "weakened", "contradicted", "unchanged", "not_reobserved")}
    ev_added: list[dict] = []
    ev_removed: list[dict] = []
    ev_stance: list[dict] = []

    for cid in sorted(set(base) | set(target)):
        b, t = base.get(cid), target.get(cid)
        if t is not None and b is None:
            classes["new"].append(claim_entry(cid))
            ev_added += [ev_entry(e[0], cid, e[1], e[2], e[3], claim_is_new=True) for e in t.evidence]
        elif b is not None and t is None:
            classes["not_reobserved"].append(claim_entry(cid))
            ev_removed += [ev_entry(e[0], cid, e[1], e[2], e[3], claim_not_reobserved=True) for e in b.evidence]
        else:
            label, reasons = classify_change(b, t)
            entry = claim_entry(cid, reasons=reasons, score=_score(b, t))
            classes[label].append(entry)
            b_ev, t_ev = {e[0]: e for e in b.evidence}, {e[0]: e for e in t.evidence}
            ev_added += [ev_entry(e[0], cid, e[1], e[2], e[3]) for k, e in sorted(t_ev.items()) if k not in b_ev]
            ev_removed += [ev_entry(e[0], cid, e[1], e[2], e[3]) for k, e in sorted(b_ev.items()) if k not in t_ev]
            for k in sorted(set(b_ev) & set(t_ev)):
                if b_ev[k][1] != t_ev[k][1]:
                    ev_stance.append(ev_entry(k, cid, t_ev[k][1], t_ev[k][2], t_ev[k][3], previous_stance=b_ev[k][1]))

    for label in ("strengthened", "weakened"):
        classes[label].sort(key=lambda e: (-abs(e["score"]), e["claim_id"]))

    # ── staleness, as of the target investigation's time ─────────────────────
    stale_claims: list[dict] = []
    stale_evidence: list[dict] = []
    stale_claim_ids: set[str] = set()
    for cid in sorted(set(target) | set(base)):
        in_target = cid in target
        obs = service._pick_obs(g, cid, target_id if in_target else None, as_of=as_of)
        if obs is None:
            continue
        assessment = service.assess_claim(g, cid, obs, as_of)
        if assessment.is_stale:
            stale_claim_ids.add(cid)
            stale_claims.append(claim_entry(cid, scope="target" if in_target else "base_only", stale=assessment.as_dict(),
                                            last_observed_at=obs.observed_at.isoformat()))
        snap = target[cid] if in_target else base[cid]
        for evidence_id, stance, strength, source_id in snap.evidence:
            fresh = source_freshness(g.sources.get(source_id, {}).get("publish_date"), as_of=as_of)["label"]
            reasons = []
            if fresh == "old":
                reasons.append("source_old")
            if service._changed_since(g, source_id, obs.report_id, obs.observed_at, as_of):
                reasons.append("source_changed")
            if reasons:
                stale_evidence.append(ev_entry(evidence_id, cid, stance, strength, source_id, reasons=reasons))

    # ── sources whose content changed between the two investigations ─────────
    changed_sources: list[dict] = []
    for sid in sorted(g.sources):
        a, b = g.source_obs.get((base_id, sid)), g.source_obs.get((target_id, sid))
        if a and b and a.content_hash and b.content_hash and a.content_hash != b.content_hash:
            affected = sorted({
                cid for cid, snap in list(base.items()) + list(target.items())
                if any(e[3] == sid for e in snap.evidence)
            })
            src = g.sources[sid]
            changed_sources.append({"source_id": sid, "url": src["url"], "domain": src["domain"],
                                    "base_hash": a.content_hash, "target_hash": b.content_hash, "affected_claim_ids": affected})

    def info(i, rid):
        return {"report_id": rid, "topic": i.topic, "created_at": i.created_at_raw, "knowledge_state": i.state}

    return {
        "base": info(base_info, base_id),
        "target": info(target_info, target_id),
        "as_of": as_of.isoformat(),
        "warnings": warnings,
        "summary": {
            **{k: len(v) for k, v in classes.items()},
            "stale": len(stale_claims),
            "evidence_added": len(ev_added), "evidence_removed": len(ev_removed),
            "evidence_stance_changed": len(ev_stance), "evidence_stale": len(stale_evidence),
            "sources_changed": len(changed_sources),
        },
        "claims": {**classes, "stale": stale_claims},
        "evidence": {"added": ev_added, "removed": ev_removed, "stance_changed": ev_stance, "stale": stale_evidence},
        "sources": {"changed": changed_sources},
    }
