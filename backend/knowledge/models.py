"""Canonical knowledge models: Source, Evidence, Claim, Entity and their links."""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from backend.models.common import BaseSchema

STANCES = ("support", "contradict", "mixed")


class Source(BaseSchema):
    source_id: str
    canonical_url: str
    url: str
    domain: str
    title: str = ""
    publish_date: Optional[str] = None


class Evidence(BaseSchema):
    evidence_id: str
    source_id: str
    passage_text: str


class Claim(BaseSchema):
    claim_id: str
    claim_text: str
    normalized_text: str


class Entity(BaseSchema):
    entity_id: str
    name: str
    canonical_name: str
    entity_type: str = "proper_noun"


class ClaimEvidenceLink(BaseSchema):
    claim_id: str
    evidence_id: str
    stance: str = "mixed"
    strength: float = 0.0


class ClaimObservation(BaseSchema):
    """What one investigation concluded about a (stable) claim."""

    claim_id: str
    status: str
    confidence: float = 0.0


class ReportSourceObservation(BaseSchema):
    source_id: str
    report_source_id: str = ""
    rank_position: int = 0
    content_hash: Optional[str] = None


class InvestigationKnowledge(BaseSchema):
    """Everything one report contributes to the graph, ready to persist atomically."""

    sources: list[Source] = Field(default_factory=list)
    report_sources: list[ReportSourceObservation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    observations: list[ClaimObservation] = Field(default_factory=list)
    claim_evidence: list[ClaimEvidenceLink] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    claim_entities: list[tuple[str, str]] = Field(default_factory=list)      # (claim_id, entity_id)
    evidence_entities: list[tuple[str, str]] = Field(default_factory=list)   # (evidence_id, entity_id)
    skipped_unresolved_evidence: int = 0
    skipped_sources: int = 0   # sources with an unusable URL
    skipped_claims: int = 0    # claims with empty text
