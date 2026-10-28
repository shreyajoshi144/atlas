from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.models.domain_models import ClaimEvidence, RankedSource, VerificationSummary, VerifiedClaim

_DATASET_PATH = Path(__file__).with_name("eval_dataset.json")


def load_dataset() -> dict[str, Any]:
    return json.loads(_DATASET_PATH.read_text(encoding="utf-8"))


def load_scenarios() -> list[dict[str, Any]]:
    return load_dataset()["scenarios"]


def build_ranked_sources(scenario: dict[str, Any]) -> list[RankedSource]:
    sources = []
    for idx, s in enumerate(scenario.get("sources", []), start=1):
        sources.append(
            RankedSource(
                title=s["title"],
                url=s["url"],
                domain=s["domain"],
                relevance_score=s.get("relevance_score", 0.0),
                credibility_score=s.get("credibility_score", 0.0),
                rank_position=idx,
            )
        )
    return sources


def build_verification_summary(scenario: dict[str, Any]) -> VerificationSummary | None:
    claims_data = scenario.get("verification_claims")
    if claims_data is None:
        return None
    claims = []
    for c in claims_data:
        evidence = [
            ClaimEvidence(
                source_id=e.get("source_id", ""),
                url=e["url"],
                stance=e.get("stance", "mixed"),
                title=e.get("title", ""),
                evidence_text=e.get("evidence_text", ""),
                evidence_strength=e.get("evidence_strength", 0.0),
            )
            for e in c.get("evidence", [])
        ]
        claims.append(
            VerifiedClaim(
                claim_text=c["claim_text"],
                confidence=c.get("confidence", 0.0),
                status=c.get("status", "insufficient_evidence"),
                evidence=evidence,
            )
        )
    return VerificationSummary(
        overall_verification_score=scenario.get("overall_verification_score", 0.0),
        verdict=scenario.get("verdict", "unverified"),
        claims=claims,
    )
