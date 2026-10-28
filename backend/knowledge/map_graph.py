"""Loads ONE user's persisted knowledge graph into an in-memory, indexed view.

Every statement is scoped by ``user_id`` (reports are scoped through the owning
research session). The result is a read-only snapshot: all Knowledge Map logic
(freshness, staleness, graph, diff) is then pure Python over this structure,
which keeps it deterministic and unit-testable.

Scale note: the graph is assembled per request. That is appropriate for a
personal research workspace (thousands of claims); a much larger store would
need SQL-side pagination or a materialised view.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from datetime import datetime
from typing import Optional

from backend.database.sqlite import SQLiteManager
from backend.knowledge.freshness import parse_dt


@dataclass(frozen=True)
class ReportInfo:
    report_id: str
    topic: str
    created_at: datetime
    created_at_raw: str
    state: str                       # complete | partial | failed | not_recorded
    error: Optional[str] = None      # INTERNAL: never serialised to clients verbatim
    dropped_sources: int = 0
    dropped_evidence: int = 0
    dropped_claims: int = 0
    state_updated_at: Optional[str] = None
    claim_count: int = 0
    source_count: int = 0


@dataclass(frozen=True)
class Obs:
    claim_id: str
    report_id: str
    status: str
    confidence: float
    observed_at: datetime


@dataclass(frozen=True)
class Link:
    report_id: str
    claim_id: str
    evidence_id: str
    stance: str
    strength: float


@dataclass(frozen=True)
class SourceObs:
    report_id: str
    source_id: str
    content_hash: Optional[str]
    rank_position: int
    observed_at: datetime


@dataclass
class UserGraph:
    user_id: str
    reports: dict[str, ReportInfo] = field(default_factory=dict)
    claims: dict[str, dict] = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)
    evidence: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, dict] = field(default_factory=dict)
    obs_by_claim: dict[str, list[Obs]] = field(default_factory=dict)          # ascending in time
    links_by_claim_report: dict[tuple[str, str], list[Link]] = field(default_factory=dict)
    links_by_evidence: dict[str, list[Link]] = field(default_factory=dict)
    claim_entity_reports: dict[tuple[str, str], set[str]] = field(default_factory=dict)  # (claim, entity) -> reports
    evidence_entity_reports: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    entity_claims: dict[str, set[str]] = field(default_factory=dict)
    claim_entities: dict[str, set[str]] = field(default_factory=dict)
    entity_evidence: dict[str, set[str]] = field(default_factory=dict)
    evidence_entities: dict[str, set[str]] = field(default_factory=dict)
    source_obs: dict[tuple[str, str], SourceObs] = field(default_factory=dict)
    source_reports: dict[str, list[SourceObs]] = field(default_factory=dict)  # ascending in time
    source_evidence: dict[str, set[str]] = field(default_factory=dict)
    # M7: challenges are separate persisted events; they never alter the observations above.
    challenges: dict[str, list[dict]] = field(default_factory=dict)          # claim_id -> rows, oldest first
    challenge_by_id: dict[str, dict] = field(default_factory=dict)
    challenge_evidence: dict[str, list[dict]] = field(default_factory=dict)  # challenge_id -> evidence rows
    counter_nodes: dict[str, dict] = field(default_factory=dict)             # cev_ id -> evidence row

    def report_state(self, report_id: str) -> str:
        info = self.reports.get(report_id)
        return info.state if info else "not_recorded"


def counter_node_id(challenge_id: str, evidence_id: str) -> str:
    """Graph id for a piece of challenge evidence, namespaced by challenge so it can
    never be confused with the investigation's own ``evd_`` evidence."""
    return "cev_" + hashlib.sha256(f"{challenge_id}\x1f{evidence_id}".encode()).hexdigest()[:32]


def load_user_graph(db: SQLiteManager, user_id: str) -> UserGraph:
    if not user_id:
        raise ValueError("user_id is required")
    g = UserGraph(user_id=user_id)
    with closing(db.get_connection()) as conn:
        def rows(sql: str, params: tuple = ()):
            return conn.execute(sql, params).fetchall()

        for r in rows(
            """
            SELECT r.report_id, r.topic, r.created_at,
                   s.status, s.error, s.dropped_sources, s.dropped_evidence, s.dropped_claims, s.updated_at AS state_updated_at,
                   (SELECT COUNT(*) FROM knowledge_claim_observations o WHERE o.report_id = r.report_id AND o.user_id = ?) AS claim_count,
                   (SELECT COUNT(*) FROM knowledge_report_sources x WHERE x.report_id = r.report_id AND x.user_id = ?) AS source_count
            FROM reports r
            JOIN research_sessions rs ON rs.session_id = r.session_id
            LEFT JOIN knowledge_investigation_status s ON s.report_id = r.report_id AND s.user_id = ?
            WHERE rs.user_id = ?
            ORDER BY r.created_at DESC, r.report_id
            """,
            (user_id, user_id, user_id, user_id),
        ):
            g.reports[r["report_id"]] = ReportInfo(
                report_id=r["report_id"], topic=r["topic"], created_at=parse_dt(r["created_at"]),
                created_at_raw=r["created_at"], state=r["status"] or "not_recorded", error=r["error"],
                dropped_sources=r["dropped_sources"] or 0, dropped_evidence=r["dropped_evidence"] or 0,
                dropped_claims=r["dropped_claims"] or 0, state_updated_at=r["state_updated_at"],
                claim_count=r["claim_count"], source_count=r["source_count"],
            )

        for r in rows("SELECT * FROM knowledge_claims WHERE user_id = ?", (user_id,)):
            g.claims[r["claim_id"]] = dict(r)
        for r in rows("SELECT * FROM knowledge_entities WHERE user_id = ?", (user_id,)):
            g.entities[r["entity_id"]] = dict(r)
        for r in rows("SELECT * FROM knowledge_evidence WHERE user_id = ?", (user_id,)):
            g.evidence[r["evidence_id"]] = dict(r)
            g.source_evidence.setdefault(r["source_id"], set()).add(r["evidence_id"])
        for r in rows("SELECT * FROM knowledge_sources WHERE user_id = ?", (user_id,)):
            g.sources[r["source_id"]] = dict(r)

        for r in rows(
            "SELECT claim_id, report_id, status, confidence FROM knowledge_claim_observations WHERE user_id = ?", (user_id,)
        ):
            info = g.reports.get(r["report_id"])
            if info is None:  # observation whose report is not this user's: ignore, never surface
                continue
            g.obs_by_claim.setdefault(r["claim_id"], []).append(
                Obs(r["claim_id"], r["report_id"], r["status"], float(r["confidence"] or 0.0), info.created_at)
            )
        for lst in g.obs_by_claim.values():
            lst.sort(key=lambda o: (o.observed_at, o.report_id))

        for r in rows(
            "SELECT report_id, claim_id, evidence_id, stance, strength FROM knowledge_claim_evidence WHERE user_id = ?", (user_id,)
        ):
            link = Link(r["report_id"], r["claim_id"], r["evidence_id"], r["stance"], float(r["strength"] or 0.0))
            g.links_by_claim_report.setdefault((link.claim_id, link.report_id), []).append(link)
            g.links_by_evidence.setdefault(link.evidence_id, []).append(link)

        for r in rows("SELECT report_id, claim_id, entity_id FROM knowledge_claim_entities WHERE user_id = ?", (user_id,)):
            g.claim_entity_reports.setdefault((r["claim_id"], r["entity_id"]), set()).add(r["report_id"])
            g.entity_claims.setdefault(r["entity_id"], set()).add(r["claim_id"])
            g.claim_entities.setdefault(r["claim_id"], set()).add(r["entity_id"])
        for r in rows("SELECT report_id, evidence_id, entity_id FROM knowledge_evidence_entities WHERE user_id = ?", (user_id,)):
            g.evidence_entity_reports.setdefault((r["evidence_id"], r["entity_id"]), set()).add(r["report_id"])
            g.entity_evidence.setdefault(r["entity_id"], set()).add(r["evidence_id"])
            g.evidence_entities.setdefault(r["evidence_id"], set()).add(r["entity_id"])

        for r in rows(
            "SELECT report_id, source_id, content_hash, rank_position FROM knowledge_report_sources WHERE user_id = ?", (user_id,)
        ):
            info = g.reports.get(r["report_id"])
            if info is None:
                continue
            so = SourceObs(r["report_id"], r["source_id"], r["content_hash"], r["rank_position"], info.created_at)
            g.source_obs[(so.report_id, so.source_id)] = so
            g.source_reports.setdefault(so.source_id, []).append(so)
        for lst in g.source_reports.values():
            lst.sort(key=lambda s: (s.observed_at, s.report_id))

        for r in rows(
            "SELECT challenge_id, investigation_id, claim_id, challenge_type, status, result, created_at, completed_at, "
            "attempt_count, error_type FROM challenges WHERE user_id = ? ORDER BY created_at, challenge_id", (user_id,)
        ):
            row = dict(r)
            g.challenges.setdefault(row["claim_id"], []).append(row)
            g.challenge_by_id[row["challenge_id"]] = row
        for r in rows(
            "SELECT e.challenge_id, e.evidence_id, e.stance, e.strength, e.passage_text, s.url, s.domain, s.independent "
            "FROM challenge_evidence e JOIN challenge_sources s ON s.challenge_id = e.challenge_id AND s.source_id = e.source_id "
            "JOIN challenges c ON c.challenge_id = e.challenge_id WHERE c.user_id = ? ORDER BY e.challenge_id, e.evidence_id", (user_id,)
        ):
            row = dict(r)
            row["node_id"] = counter_node_id(row["challenge_id"], row["evidence_id"])
            g.challenge_evidence.setdefault(row["challenge_id"], []).append(row)
            g.counter_nodes[row["node_id"]] = row
    return g
