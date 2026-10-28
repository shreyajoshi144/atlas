"""Knowledge Map service (M6): authorised queries over the persisted graph.

All reads go through :func:`load_user_graph`, which is scoped to one user. An ID
that does not exist *or belongs to someone else* raises the same
:class:`NotFoundError`, so existence is never leaked across users.

Knowledge state is explicit on every object (``graph_state`` + ``is_complete``):
``complete`` / ``partial`` / ``failed`` / ``not_recorded``. A report whose
persistence failed is never presented as complete, and the comparison workflow
refuses to treat it as input.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

from backend.database import ResearchRepository
from backend.challenges.classify import interpret
from backend.knowledge.changes import compare_investigations
from backend.knowledge.freshness import (
    DEFAULT_SOURCE_MAX_AGE_DAYS,
    DEFAULT_STALE_AFTER_DAYS,
    STATE_ORDER,
    StaleAssessment,
    SupportingSource,
    assess_staleness,
    source_freshness,
    worst_state,
)
from backend.knowledge.map_graph import Obs, UserGraph, load_user_graph
from backend.knowledge.repository import KnowledgeRepository

VERIFIED_STATUSES = ("verified", "partially_verified")
MAX_LIMIT = 100


class NotFoundError(LookupError):
    """Object does not exist for this user (or exists for someone else)."""


class ChangeComparisonError(Exception):
    """The two investigations cannot be compared (e.g. a failed knowledge write)."""

    def __init__(self, reason: str, report_id: str | None = None, state: str | None = None):
        super().__init__(reason)
        self.reason, self.report_id, self.state = reason, report_id, state


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _page(items: list, limit: int, offset: int) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    return {"items": items[offset: offset + limit], "total": len(items), "limit": limit, "offset": offset}


def _needle(query: str) -> str:
    """Case-insensitive, whitespace-collapsed literal search text ('' = no filter).

    Deliberately NOT punctuation-stripped: a non-blank query such as ``%`` must
    match literal text, never degrade into 'match everything'.
    """
    return " ".join((query or "").lower().split())


def _error_type(error: Optional[str]) -> Optional[str]:
    """Expose only the exception class name — raw messages can carry DB internals."""
    return error.split(":", 1)[0].strip() if error else None


class KnowledgeMapService:
    def __init__(
        self,
        repository: ResearchRepository,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
        source_max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS,
    ) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_days = stale_after_days
        self.source_max_age_days = source_max_age_days

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _graph(self, user_id: str) -> UserGraph:
        return load_user_graph(self.repository.db, user_id)

    def _now(self) -> datetime:
        return self._clock()

    # ── shared computations ──────────────────────────────────────────────────
    def _changed_since(self, g: UserGraph, source_id: str, report_id: str, observed_at: datetime, as_of: datetime) -> bool:
        """Did this source's content hash change in a later investigation (≤ as_of)?"""
        base = g.source_obs.get((report_id, source_id))
        if base is None or not base.content_hash:
            return False
        return any(
            so.content_hash and so.content_hash != base.content_hash and observed_at < so.observed_at <= as_of
            for so in g.source_reports.get(source_id, [])
        )

    def _pick_obs(self, g: UserGraph, claim_id: str, report_id: Optional[str], as_of: Optional[datetime] = None) -> Optional[Obs]:
        observations = [o for o in g.obs_by_claim.get(claim_id, []) if as_of is None or o.observed_at <= as_of]
        if report_id is not None:
            return next((o for o in observations if o.report_id == report_id), None)
        return observations[-1] if observations else None

    def _claim_links(self, g: UserGraph, claim_id: str, report_id: str):
        return g.links_by_claim_report.get((claim_id, report_id), [])

    def assess_claim(self, g: UserGraph, claim_id: str, obs: Obs, as_of: datetime) -> StaleAssessment:
        supporting = {
            l.evidence_id: g.evidence[l.evidence_id]["source_id"]
            for l in self._claim_links(g, claim_id, obs.report_id)
            if l.stance == "support" and l.evidence_id in g.evidence
        }
        sources = []
        for sid in sorted(set(supporting.values())):
            src = g.sources.get(sid)
            sources.append(SupportingSource(
                source_id=sid, publish_date=src.get("publish_date") if src else None,
                changed_since_verified=self._changed_since(g, sid, obs.report_id, obs.observed_at, as_of),
            ))
        return assess_staleness(
            obs.observed_at, sources, as_of=as_of,
            stale_after_days=self.stale_after_days, source_max_age_days=self.source_max_age_days,
        )

    def _source_ref(self, g: UserGraph, source_id: str, as_of: datetime, *, claim_obs: Optional[Obs] = None) -> dict:
        src = g.sources.get(source_id, {})
        ref = {
            "source_id": source_id, "url": src.get("url"), "domain": src.get("domain"), "title": src.get("title", ""),
            "publish_date": src.get("publish_date"), "freshness": source_freshness(src.get("publish_date"), as_of=as_of),
            "last_seen_at": src.get("last_seen_at"),
        }
        if claim_obs is not None:
            ref["changed_since_verified"] = self._changed_since(g, source_id, claim_obs.report_id, claim_obs.observed_at, as_of)
        return ref

    def _evidence_state(self, g: UserGraph, evidence_id: str) -> str:
        return worst_state(g.report_state(l.report_id) for l in g.links_by_evidence.get(evidence_id, []))

    def _source_state(self, g: UserGraph, source_id: str) -> str:
        reports = {so.report_id for so in g.source_reports.get(source_id, [])}
        for eid in g.source_evidence.get(source_id, ()):
            reports |= {l.report_id for l in g.links_by_evidence.get(eid, [])}
        return worst_state(g.report_state(r) for r in reports)

    def _entity_state(self, g: UserGraph, entity_id: str) -> str:
        reports: set[str] = set()
        for cid in g.entity_claims.get(entity_id, ()):
            reports |= g.claim_entity_reports.get((cid, entity_id), set())
        for eid in g.entity_evidence.get(entity_id, ()):
            reports |= g.evidence_entity_reports.get((eid, entity_id), set())
        return worst_state(g.report_state(r) for r in reports)

    def _claim_summary(self, g: UserGraph, claim_id: str, as_of: datetime, report_id: Optional[str] = None) -> Optional[dict]:
        obs = self._pick_obs(g, claim_id, report_id)
        if obs is None:
            return None
        links = self._claim_links(g, claim_id, obs.report_id)
        stances = Counter(l.stance for l in links)
        verified = [o for o in g.obs_by_claim[claim_id] if o.status in VERIFIED_STATUSES and o.observed_at <= as_of]
        state = g.report_state(obs.report_id)
        assessment = self.assess_claim(g, claim_id, obs, as_of)
        return {
            "claim_id": claim_id,
            "text": g.claims[claim_id]["claim_text"],
            "status": obs.status,
            "confidence": obs.confidence,
            "investigation": {"report_id": obs.report_id, "topic": g.reports[obs.report_id].topic},
            "observed_at": _iso(obs.observed_at),
            "last_verified_at": _iso(verified[-1].observed_at) if verified else None,
            "first_seen_at": _iso(g.obs_by_claim[claim_id][0].observed_at),
            "observation_count": len(g.obs_by_claim[claim_id]),
            "evidence_counts": {
                "supporting": stances.get("support", 0), "conflicting": stances.get("contradict", 0),
                "mixed": stances.get("mixed", 0),
            },
            "stale": assessment.as_dict(),
            "graph_state": state,
            "is_complete": state == "complete",
            "challenges": self._challenge_counts(g, claim_id),
            "interpretation": self._interpretation(g, claim_id, obs),
        }

    # ── challenges (M7): additive views; observations/evidence are never modified ──
    @staticmethod
    def _challenge_counts(g: UserGraph, claim_id: str) -> dict:
        rows = g.challenges.get(claim_id, [])
        done = [r for r in rows if r["status"] == "completed"]
        return {"total": len(rows), "completed": len(done), "failed": sum(r["status"] == "failed" for r in rows),
                "active": sum(r["status"] in ("pending", "running") for r in rows),
                "latest_result": done[-1]["result"] if done else None}

    @staticmethod
    def _interpretation(g: UserGraph, claim_id: str, obs: Obs) -> dict:
        """Updated interpretation = original observation + completed challenges (a view; nothing is overwritten)."""
        return interpret(obs.status, obs.confidence, g.challenges.get(claim_id, []))

    def _challenge_views(self, g: UserGraph, claim_id: str) -> list[dict]:
        out = []
        for r in sorted(g.challenges.get(claim_id, []), key=lambda r: (r["created_at"], r["challenge_id"]), reverse=True):
            ev = g.challenge_evidence.get(r["challenge_id"], [])
            out.append({
                "challenge_id": r["challenge_id"], "investigation_id": r["investigation_id"], "challenge_type": r["challenge_type"],
                "status": r["status"], "result": r["result"], "created_at": r["created_at"], "completed_at": r["completed_at"],
                "attempt_count": r["attempt_count"], "error_type": r["error_type"],
                "counter_evidence_count": sum(1 for e in ev if e["stance"] == "contradict"), "evidence_count": len(ev),
                "graph_state": g.report_state(r["investigation_id"]),
            })
        return out

    def _challenge_trail(self, g: UserGraph, claim_view: dict, supporting: int, conflicting: int) -> Optional[list[dict]]:
        """Claim -> Evidence -> Challenge -> Counter-evidence -> Result -> Updated interpretation (latest completed challenge)."""
        done = [r for r in g.challenges.get(claim_view["claim_id"], []) if r["status"] == "completed"]
        if not done:
            return None
        ch, interp = done[-1], claim_view["interpretation"]
        ev = g.challenge_evidence.get(ch["challenge_id"], [])
        return [
            {"step": "claim", "text": claim_view["text"], "status": claim_view["status"], "confidence": claim_view["confidence"]},
            {"step": "evidence", "supporting": supporting, "conflicting": conflicting},
            {"step": "challenge", "challenge_id": ch["challenge_id"], "challenge_type": ch["challenge_type"]},
            {"step": "counter_evidence", "items": [{"evidence_id": e["evidence_id"], "stance": e["stance"], "strength": e["strength"],
                                                    "url": e["url"], "independent": bool(e["independent"])} for e in ev]},
            {"step": "result", "value": ch["result"]},
            {"step": "updated_interpretation", "label": interp["label"], "summary": interp["summary"],
             "adjusted_confidence": interp["adjusted_confidence"]},
        ]

    @staticmethod
    def _challenge_summary(g: UserGraph) -> dict:
        rows = list(g.challenge_by_id.values())
        return {"total": len(rows), "by_status": dict(Counter(r["status"] for r in rows)),
                "by_result": dict(Counter(r["result"] for r in rows if r["result"]))}

    # ── investigations / summary ─────────────────────────────────────────────
    def list_investigations(self, user_id: str) -> list[dict]:
        g = self._graph(user_id)
        return [self._investigation_view(info) for info in g.reports.values()]

    @staticmethod
    def _investigation_view(info) -> dict:
        return {
            "report_id": info.report_id, "topic": info.topic, "created_at": info.created_at_raw,
            "knowledge_state": info.state,
            "is_complete": info.state == "complete",
            "comparable": info.state in ("complete", "partial"),
            "claim_count": info.claim_count, "source_count": info.source_count,
            "dropped": {"sources": info.dropped_sources, "evidence": info.dropped_evidence, "claims": info.dropped_claims},
            "error_type": _error_type(info.error),
            "state_updated_at": info.state_updated_at,
        }

    def summary(self, user_id: str) -> dict:
        g = self._graph(user_id)
        as_of = self._now()
        states = Counter(info.state for info in g.reports.values())
        stale = conflicting = 0
        for cid in g.claims:
            view = self._claim_summary(g, cid, as_of)
            if view is None:
                continue
            stale += view["stale"]["is_stale"]
            conflicting += view["status"] == "conflicting" or view["evidence_counts"]["conflicting"] > 0
        problems = KnowledgeRepository(self.repository.db).integrity_problems(user_id=user_id)
        return {
            "counts": {"entities": len(g.entities), "claims": len(g.claims), "evidence": len(g.evidence), "sources": len(g.sources)},
            "investigations": {"total": len(g.reports), **{s: states.get(s, 0) for s in STATE_ORDER}},
            # True only when EVERY investigation's knowledge is complete.
            "map_is_complete": bool(g.reports) and states.get("complete", 0) == len(g.reports),
            "stale_claims": stale,
            "conflicting_claims": conflicting,
            "integrity": {"ok": not problems, "problem_count": len(problems)},
            "challenges": self._challenge_summary(g),
            "generated_at": _iso(as_of),
        }

    # ── entities ─────────────────────────────────────────────────────────────
    def _entity_summary(self, g: UserGraph, entity_id: str) -> dict:
        e = g.entities[entity_id]
        evidence = g.entity_evidence.get(entity_id, set())
        state = self._entity_state(g, entity_id)
        return {
            "entity_id": entity_id, "name": e["name"], "entity_type": e["entity_type"],
            "claim_count": len(g.entity_claims.get(entity_id, ())), "evidence_count": len(evidence),
            "source_count": len({g.evidence[v]["source_id"] for v in evidence if v in g.evidence}),
            "graph_state": state, "is_complete": state == "complete",
        }

    def list_entities(self, user_id: str, *, query: str = "", limit: int = 25, offset: int = 0) -> dict:
        g = self._graph(user_id)
        needle = _needle(query)
        items = [
            self._entity_summary(g, eid) for eid, e in g.entities.items()
            if not needle or needle in e["name"].lower() or needle in e["canonical_name"]
        ]
        items.sort(key=lambda i: (-i["claim_count"], i["name"].lower(), i["entity_id"]))
        return _page(items, limit, offset)

    def get_entity(self, user_id: str, entity_id: str) -> dict:
        g = self._graph(user_id)
        if entity_id not in g.entities:
            raise NotFoundError("entity not found")
        as_of = self._now()
        direct = set(g.entity_claims.get(entity_id, ()))
        via_evidence: set[str] = set()
        for vid in g.entity_evidence.get(entity_id, ()):
            via_evidence |= {l.claim_id for l in g.links_by_evidence.get(vid, [])}
        claims = []
        for cid in sorted(direct | via_evidence):
            view = self._claim_summary(g, cid, as_of)
            if view:
                view["relation"] = "mentioned" if cid in direct else "evidence_mentions"
                claims.append(view)
        claims.sort(key=lambda c: (-c["confidence"], c["claim_id"]))
        sources = sorted({g.evidence[v]["source_id"] for v in g.entity_evidence.get(entity_id, ()) if v in g.evidence})
        return {
            **self._entity_summary(g, entity_id),
            "claims": claims,
            "sources": [self._source_ref(g, sid, as_of) for sid in sources],
        }

    # ── claims ───────────────────────────────────────────────────────────────
    def list_claims(
        self, user_id: str, *, query: str = "", status: Optional[str] = None, stale: Optional[bool] = None,
        conflicting: Optional[bool] = None, report_id: Optional[str] = None, entity_id: Optional[str] = None,
        limit: int = 25, offset: int = 0,
    ) -> dict:
        g = self._graph(user_id)
        if report_id is not None and report_id not in g.reports:
            raise NotFoundError("investigation not found")
        if entity_id is not None and entity_id not in g.entities:
            raise NotFoundError("entity not found")
        as_of = self._now()
        needle = _needle(query)
        scope = g.entity_claims.get(entity_id) if entity_id else None
        items = []
        for cid, c in g.claims.items():
            if scope is not None and cid not in scope:
                continue
            if needle and needle not in " ".join(c["claim_text"].lower().split()):
                continue
            view = self._claim_summary(g, cid, as_of, report_id=report_id)
            if view is None:
                continue
            if status and view["status"] != status:
                continue
            if stale is not None and view["stale"]["is_stale"] != stale:
                continue
            if conflicting is not None:
                is_conf = view["status"] == "conflicting" or view["evidence_counts"]["conflicting"] > 0
                if is_conf != conflicting:
                    continue
            items.append(view)
        items.sort(key=lambda v: (v["observed_at"] or "", v["claim_id"]), reverse=True)
        return _page(items, limit, offset)

    def get_claim(self, user_id: str, claim_id: str, *, report_id: Optional[str] = None) -> dict:
        g = self._graph(user_id)
        if claim_id not in g.claims:
            raise NotFoundError("claim not found")
        as_of = self._now()
        view = self._claim_summary(g, claim_id, as_of, report_id=report_id)
        if view is None:
            raise NotFoundError("claim was not observed in that investigation")
        obs = self._pick_obs(g, claim_id, report_id)
        grouped: dict[str, list] = {"supporting": [], "conflicting": [], "mixed": []}
        bucket = {"support": "supporting", "contradict": "conflicting"}
        for l in sorted(self._claim_links(g, claim_id, obs.report_id), key=lambda x: (-x.strength, x.evidence_id)):
            ev = g.evidence.get(l.evidence_id)
            if ev is None:
                continue
            grouped[bucket.get(l.stance, "mixed")].append({
                "evidence_id": l.evidence_id, "passage": ev["passage_text"], "stance": l.stance, "strength": l.strength,
                "source": self._source_ref(g, ev["source_id"], as_of, claim_obs=obs),
                "graph_state": self._evidence_state(g, l.evidence_id),
            })
        history = [
            {"report_id": o.report_id, "topic": g.reports[o.report_id].topic, "observed_at": _iso(o.observed_at),
             "status": o.status, "confidence": o.confidence, "graph_state": g.report_state(o.report_id)}
            for o in g.obs_by_claim[claim_id]
        ]
        entities = [
            {"entity_id": eid, "name": g.entities[eid]["name"], "entity_type": g.entities[eid]["entity_type"]}
            for eid in sorted(g.claim_entities.get(claim_id, ())) if eid in g.entities
        ]
        return {
            **view, "evidence": grouped, "history": history, "entities": entities,
            "challenges_detail": self._challenge_views(g, claim_id),
            "challenge_trail": self._challenge_trail(g, view, len(grouped["supporting"]), len(grouped["conflicting"])),
        }

    # ── evidence & sources ───────────────────────────────────────────────────
    def get_evidence(self, user_id: str, evidence_id: str) -> dict:
        g = self._graph(user_id)
        ev = g.evidence.get(evidence_id)
        if ev is None:
            raise NotFoundError("evidence not found")
        as_of = self._now()
        claims = []
        for cid in sorted({l.claim_id for l in g.links_by_evidence.get(evidence_id, [])}):
            obs = self._pick_obs(g, cid, None)
            current = [l for l in self._claim_links(g, cid, obs.report_id) if l.evidence_id == evidence_id] if obs else []
            summary = self._claim_summary(g, cid, as_of)
            if summary:
                summary["stance"] = current[0].stance if current else None
                summary["strength"] = current[0].strength if current else None
                summary["in_current_investigation"] = bool(current)
                claims.append(summary)
        return {
            "evidence_id": evidence_id, "passage": ev["passage_text"],
            "source": self._source_ref(g, ev["source_id"], as_of), "claims": claims,
            "entities": [
                {"entity_id": eid, "name": g.entities[eid]["name"]}
                for eid in sorted(g.evidence_entities.get(evidence_id, ())) if eid in g.entities
            ],
            "graph_state": self._evidence_state(g, evidence_id),
            "is_complete": self._evidence_state(g, evidence_id) == "complete",
        }

    def _source_summary(self, g: UserGraph, source_id: str, as_of: datetime) -> dict:
        ref = self._source_ref(g, source_id, as_of)
        hashes = {so.content_hash for so in g.source_reports.get(source_id, []) if so.content_hash}
        state = self._source_state(g, source_id)
        ref.update({
            "canonical_url": g.sources[source_id]["canonical_url"],
            "first_seen_at": g.sources[source_id]["first_seen_at"],
            "observation_count": len(g.source_reports.get(source_id, [])),
            "evidence_count": len(g.source_evidence.get(source_id, ())),
            "content_changed": len(hashes) > 1,
            "graph_state": state, "is_complete": state == "complete",
        })
        return ref

    def list_sources(self, user_id: str, *, query: str = "", freshness: Optional[str] = None, limit: int = 25, offset: int = 0) -> dict:
        g = self._graph(user_id)
        as_of = self._now()
        needle = _needle(query)
        items = []
        for sid, s in g.sources.items():
            if needle and needle not in f"{s['url']} {s['domain']} {s['title']}".lower():
                continue
            view = self._source_summary(g, sid, as_of)
            if freshness and view["freshness"]["label"] != freshness:
                continue
            items.append(view)
        items.sort(key=lambda v: (v["last_seen_at"] or "", v["source_id"]), reverse=True)
        return _page(items, limit, offset)

    def get_source(self, user_id: str, source_id: str) -> dict:
        g = self._graph(user_id)
        if source_id not in g.sources:
            raise NotFoundError("source not found")
        as_of = self._now()
        evidence = []
        for vid in sorted(g.source_evidence.get(source_id, ())):
            links = g.links_by_evidence.get(vid, [])
            evidence.append({
                "evidence_id": vid, "passage": g.evidence[vid]["passage_text"],
                "claim_ids": sorted({l.claim_id for l in links}), "graph_state": self._evidence_state(g, vid),
            })
        observed = [
            {"report_id": so.report_id, "topic": g.reports[so.report_id].topic, "observed_at": _iso(so.observed_at),
             "content_hash": so.content_hash, "graph_state": g.report_state(so.report_id)}
            for so in g.source_reports.get(source_id, [])
        ]
        return {**self._source_summary(g, source_id, as_of), "evidence": evidence, "observations": observed}

    # ── graph payload ────────────────────────────────────────────────────────
    @staticmethod
    def node_type(node_id: str) -> Optional[str]:
        return {"ent_": "entity", "clm_": "claim", "evd_": "evidence", "src_": "source", "chl_": "challenge", "cev_": "counter"}.get(node_id[:4])

    def graph(self, user_id: str, *, focus: Optional[str] = None, depth: int = 1, limit: int = 80) -> dict:
        g = self._graph(user_id)
        as_of = self._now()
        depth = max(1, min(int(depth), 3))
        limit = max(5, min(int(limit), 300))

        def current_links(cid: str):
            obs = self._pick_obs(g, cid, None)
            return self._claim_links(g, cid, obs.report_id) if obs else []

        def exists(nid: str) -> bool:
            return nid in {"entity": g.entities, "claim": g.claims, "evidence": g.evidence, "source": g.sources,
                           "challenge": g.challenge_by_id, "counter": g.counter_nodes}.get(self.node_type(nid) or "", {})

        stance_edge = {"support": "supported_by", "contradict": "contradicted_by"}

        def neighbours(nid: str) -> list[tuple[str, str, str, str]]:
            """(neighbour_id, edge_type, edge_source, edge_target)"""
            out: list[tuple[str, str, str, str]] = []
            kind = self.node_type(nid)
            if kind == "entity":
                out += [(c, "mentions", c, nid) for c in sorted(g.entity_claims.get(nid, ()))]
                out += [(v, "mentions", v, nid) for v in sorted(g.entity_evidence.get(nid, ()))]
            elif kind == "claim":
                out += [(e, "mentions", nid, e) for e in sorted(g.claim_entities.get(nid, ()))]
                out += [(l.evidence_id, stance_edge.get(l.stance, "mixed_with"), nid, l.evidence_id)
                        for l in sorted(current_links(nid), key=lambda x: x.evidence_id)]
                out += [(r["challenge_id"], "challenged_by", nid, r["challenge_id"]) for r in g.challenges.get(nid, [])]
            elif kind == "evidence":
                for l in sorted(g.links_by_evidence.get(nid, []), key=lambda x: x.claim_id):
                    obs = self._pick_obs(g, l.claim_id, None)
                    if obs and obs.report_id == l.report_id:
                        out.append((l.claim_id, stance_edge.get(l.stance, "mixed_with"), l.claim_id, nid))
                out.append((g.evidence[nid]["source_id"], "from_source", nid, g.evidence[nid]["source_id"]))
                out += [(e, "mentions", nid, e) for e in sorted(g.evidence_entities.get(nid, ()))]
            elif kind == "source":
                out += [(v, "from_source", v, nid) for v in sorted(g.source_evidence.get(nid, ()))]
            elif kind == "challenge":
                row = g.challenge_by_id[nid]
                out.append((row["claim_id"], "challenged_by", row["claim_id"], nid))
                role = {"contradict": "counter_evidence", "support": "supporting_evidence"}
                out += [(e["node_id"], role.get(e["stance"], "qualifying_evidence"), nid, e["node_id"])
                        for e in g.challenge_evidence.get(nid, [])]
            elif kind == "counter":
                e = g.counter_nodes[nid]
                role = {"contradict": "counter_evidence", "support": "supporting_evidence"}
                out.append((e["challenge_id"], role.get(e["stance"], "qualifying_evidence"), e["challenge_id"], nid))
            return [n for n in out if exists(n[0])]

        if focus is not None:
            if not exists(focus):
                raise NotFoundError("node not found")
            seeds = [focus]
        else:
            top = sorted(g.entities, key=lambda e: (-len(g.entity_claims.get(e, ())), g.entities[e]["name"].lower(), e))[:8]
            seeds = top or sorted(g.claims)[:10]

        order: list[str] = []
        seen: set[str] = set()
        edges: list[tuple[str, str, str]] = []
        edge_seen: set[tuple[str, str, str]] = set()
        truncated = len(seeds) > limit  # the node cap applies to seeds too
        frontier = list(seeds[:limit])
        for s in frontier:
            if s not in seen:
                seen.add(s)
                order.append(s)
        for _ in range(depth if focus is not None else 3):
            nxt: list[str] = []
            for nid in frontier:
                for neighbour, etype, src, tgt in neighbours(nid):
                    if neighbour not in seen:
                        if len(order) >= limit:
                            truncated = True
                            continue
                        seen.add(neighbour)
                        order.append(neighbour)
                        nxt.append(neighbour)
                    key = (src, tgt, etype)
                    if key not in edge_seen:
                        edge_seen.add(key)
                        edges.append(key)
            frontier = nxt
            if not frontier:
                break

        included = set(order)
        nodes = [self._graph_node(g, nid, as_of) for nid in order]
        return {
            "focus": focus, "depth": depth, "truncated": truncated,
            "nodes": nodes,
            # Edges only between included nodes: the payload never dangles.
            "edges": [{"source": s, "target": t, "type": e} for s, t, e in edges if s in included and t in included],
        }

    def _graph_node(self, g: UserGraph, nid: str, as_of: datetime) -> dict:
        kind = self.node_type(nid)
        if kind == "entity":
            e = g.entities[nid]
            return {"id": nid, "type": kind, "label": e["name"], "graph_state": self._entity_state(g, nid), "meta": {"entity_type": e["entity_type"]}}
        if kind == "claim":
            v = self._claim_summary(g, nid, as_of)
            text = g.claims[nid]["claim_text"]
            return {"id": nid, "type": kind, "label": text if len(text) <= 110 else text[:107] + "…", "graph_state": v["graph_state"],
                    "meta": {"status": v["status"], "confidence": v["confidence"], "stale": v["stale"]["is_stale"],
                             "conflicting": v["evidence_counts"]["conflicting"]}}
        if kind == "evidence":
            text = g.evidence[nid]["passage_text"]
            return {"id": nid, "type": kind, "label": text if len(text) <= 90 else text[:87] + "…",
                    "graph_state": self._evidence_state(g, nid), "meta": {}}
        if kind == "challenge":
            r = g.challenge_by_id[nid]
            outcome = r["result"] or r["status"]
            return {"id": nid, "type": kind, "label": f"{r['challenge_type'].replace('_', ' ')}: {outcome.replace('_', ' ')}",
                    "graph_state": g.report_state(r["investigation_id"]),
                    "meta": {"challenge_type": r["challenge_type"], "status": r["status"], "result": r["result"]}}
        if kind == "counter":
            e = g.counter_nodes[nid]
            text = e["passage_text"]
            return {"id": nid, "type": kind, "label": text if len(text) <= 90 else text[:87] + "…",
                    "graph_state": g.report_state(g.challenge_by_id[e["challenge_id"]]["investigation_id"]),
                    "meta": {"stance": e["stance"], "strength": e["strength"], "independent": bool(e["independent"]), "url": e["url"]}}
        s = g.sources[nid]
        return {"id": nid, "type": "source", "label": s["domain"] or s["url"], "graph_state": self._source_state(g, nid),
                "meta": {"freshness": source_freshness(s.get("publish_date"), as_of=as_of)["label"], "url": s["url"]}}

    # ── what changed ─────────────────────────────────────────────────────────
    def what_changed(self, user_id: str, base_report_id: str, target_report_id: str) -> dict:
        if base_report_id == target_report_id:
            raise ValueError("base and target must be two different investigations")
        g = self._graph(user_id)
        for rid in (base_report_id, target_report_id):
            if rid not in g.reports:
                raise NotFoundError("investigation not found")
        warnings: list[str] = []
        for rid in (base_report_id, target_report_id):
            info = g.reports[rid]
            if info.state in ("failed", "not_recorded"):
                raise ChangeComparisonError(
                    f"Knowledge for investigation {rid} is '{info.state}'; it cannot be compared until it is rebuilt.",
                    report_id=rid, state=info.state,
                )
            if info.state == "partial":
                warnings.append(f"Investigation {rid} has partial knowledge (some inputs were dropped); results may be incomplete.")
        return compare_investigations(
            self, g, base_report_id, target_report_id, warnings=warnings,
        )

    # ── recovery ─────────────────────────────────────────────────────────────
    def rebuild(self, user_id: str, report_id: str) -> dict:
        """Re-derive a report's graph from its stored sources + verification JSON."""
        from backend.knowledge.service import KnowledgeService

        stats = KnowledgeService(self.repository).backfill_report(user_id, report_id)
        if stats is None:
            raise NotFoundError("investigation not found")
        return {"report_id": report_id, **stats}
