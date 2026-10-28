"""Knowledge foundation (M5): stable IDs, models and persisted relationships
between Source, Evidence, Claim and Entity. Import-light by design."""
from backend.knowledge.ids import canonicalize_url, claim_id, entity_id, evidence_id, normalize_text, source_id
from backend.knowledge.models import (
    Claim, ClaimEvidenceLink, ClaimObservation, Entity, Evidence, InvestigationKnowledge, Source,
)

__all__ = [
    "Claim", "ClaimEvidenceLink", "ClaimObservation", "Entity", "Evidence", "InvestigationKnowledge",
    "Source", "canonicalize_url", "claim_id", "entity_id", "evidence_id", "normalize_text", "source_id",
]
