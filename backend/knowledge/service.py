"""Builds the knowledge graph for an investigation and persists it."""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.knowledge import ids
from backend.knowledge.extraction import EntityExtractor, HeuristicEntityExtractor
from backend.knowledge.models import (
    STANCES, Claim, ClaimEvidenceLink, ClaimObservation, Entity, Evidence,
    InvestigationKnowledge, ReportSourceObservation, Source,
)
from backend.knowledge.repository import STATUS_COMPLETE, KnowledgeRepository

logger = get_logger(__name__)


def _dump(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    return obj.model_dump(mode="json")


def build_investigation_knowledge(
    user_id: str,
    sources: Iterable[Any],
    claims: Iterable[Any],
    *,
    extractor: Optional[EntityExtractor] = None,
    content_hashes: Optional[Mapping[str, str]] = None,
) -> InvestigationKnowledge:
    """Pure function: retrieved sources + verified claims → graph objects.

    Evidence is persisted only if its URL resolves to a source that was actually
    retrieved in this investigation. Evidence citing any other URL (e.g. one an
    LLM invented) is counted in ``skipped_unresolved_evidence`` and dropped, so
    hallucinated citations can never become knowledge.
    """
    extractor = extractor or HeuristicEntityExtractor()
    content_hashes = content_hashes or {}
    kn = InvestigationKnowledge()
    by_canonical: dict[str, Source] = {}

    for raw in sources:
        s = _dump(raw)
        url = str(s.get("url") or "")
        try:
            canon = ids.canonicalize_url(url)
        except ValueError:
            logger.warning("Skipping source with unusable URL", extra={"url": url})
            kn.skipped_sources += 1
            continue
        if canon in by_canonical:
            continue
        src = Source(
            source_id=ids.source_id(user_id, url), canonical_url=canon, url=url,
            domain=str(s.get("domain") or ""), title=str(s.get("title") or ""),
            publish_date=str(s["publish_date"]) if s.get("publish_date") else None,
        )
        by_canonical[canon] = src
        kn.sources.append(src)
        kn.report_sources.append(ReportSourceObservation(
            source_id=src.source_id, report_source_id=str(s.get("source_id") or ""),
            rank_position=int(s.get("rank_position") or 0),
            content_hash=content_hashes.get(canon) or content_hashes.get(url),
        ))

    entities: dict[str, Entity] = {}

    def ent_ids(text: str) -> list[str]:
        out = []
        for name, etype in extractor.extract(text):
            try:
                eid = ids.entity_id(user_id, name)
            except ValueError:
                continue
            entities.setdefault(eid, Entity(entity_id=eid, name=name, canonical_name=ids.normalize_text(name), entity_type=etype))
            out.append(eid)
        return out

    seen_claims: set[str] = set()
    seen_evidence: dict[str, Evidence] = {}
    links: dict[tuple[str, str], ClaimEvidenceLink] = {}
    ce_pairs: set[tuple[str, str]] = set()
    ee_pairs: set[tuple[str, str]] = set()

    for raw in claims:
        c = _dump(raw)
        text = str(c.get("claim_text") or "").strip()
        if not ids.normalize_text(text):
            kn.skipped_claims += 1
            continue
        cid = ids.claim_id(user_id, text)
        if cid not in seen_claims:
            seen_claims.add(cid)
            kn.claims.append(Claim(claim_id=cid, claim_text=text, normalized_text=ids.normalize_text(text)))
            kn.observations.append(ClaimObservation(
                claim_id=cid, status=str(c.get("status") or "insufficient_evidence"),
                confidence=float(c.get("confidence") or 0.0)))
            for eid in ent_ids(text):
                ce_pairs.add((cid, eid))
        for ev in c.get("evidence") or []:
            e = _dump(ev)
            passage = str(e.get("evidence_text") or "").strip()
            try:
                canon = ids.canonicalize_url(str(e.get("url") or ""))
            except ValueError:
                canon = ""
            src = by_canonical.get(canon)
            if src is None or not ids.normalize_text(passage):
                kn.skipped_unresolved_evidence += 1
                continue
            evid = ids.evidence_id(user_id, src.source_id, passage)
            seen_evidence.setdefault(evid, Evidence(evidence_id=evid, source_id=src.source_id, passage_text=passage))
            stance = str(e.get("stance") or "mixed")
            stance = stance if stance in STANCES else "mixed"
            strength = min(max(float(e.get("evidence_strength") or 0.0), 0.0), 1.0)
            prev = links.get((cid, evid))
            if prev is None or strength > prev.strength:
                links[(cid, evid)] = ClaimEvidenceLink(claim_id=cid, evidence_id=evid, stance=stance, strength=strength)
            for eid in ent_ids(passage):
                ee_pairs.add((evid, eid))

    kn.evidence = list(seen_evidence.values())
    kn.claim_evidence = list(links.values())
    kn.entities = list(entities.values())
    kn.claim_entities = sorted(ce_pairs)
    kn.evidence_entities = sorted(ee_pairs)
    return kn


class KnowledgeService:
    def __init__(self, repository: ResearchRepository, extractor: Optional[EntityExtractor] = None) -> None:
        self.repository = repository
        self.knowledge = KnowledgeRepository(repository.db)
        self.extractor = extractor or HeuristicEntityExtractor()

    def record_investigation(
        self, user_id: str, report_id: str, sources: Iterable[Any], claims: Iterable[Any],
        *, scraped_contents: Iterable[Any] = (),
    ) -> dict[str, int]:
        hashes: dict[str, str] = {}
        for sc in scraped_contents:
            d = _dump(sc)
            if d.get("content_hash") and d.get("url"):
                try:
                    hashes[ids.canonicalize_url(str(d["url"]))] = d["content_hash"]
                except ValueError:
                    pass
        try:
            kn = build_investigation_knowledge(user_id, sources, claims, extractor=self.extractor, content_hashes=hashes)
        except Exception as exc:
            # A build failure must not leave a previous graph looking complete.
            self.knowledge.record_failure(user_id, report_id, exc)
            raise
        stats = self.knowledge.replace_investigation(user_id, report_id, kn)  # records 'failed' itself if it raises
        if stats["status"] != STATUS_COMPLETE:
            logger.warning("Knowledge graph recorded as partial: some inputs were dropped", extra={"report_id": report_id, **stats})
        return stats

    def get_status(self, user_id: str, report_id: str) -> dict[str, Any]:
        """Explicit graph state for a report; ``not_recorded`` if never attempted."""
        return self.knowledge.get_status(user_id, report_id) or {"status": "not_recorded", "report_id": report_id}

    def backfill_report(self, user_id: str, report_id: str) -> Optional[dict[str, int]]:
        """Rebuild a pre-M5 report's graph from its stored sources + verification JSON."""
        row = self.repository.reports.get_report(report_id=report_id, user_id=user_id)
        if not row:
            return None
        try:
            verification = json.loads(row.get("verification_json") or "{}")
        except json.JSONDecodeError:
            verification = {}
        sources = self.repository.sources.get_sources_by_report_id(report_id)
        return self.record_investigation(user_id, report_id, sources, verification.get("claims") or [])
