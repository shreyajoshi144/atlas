from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_chat_llm


@dataclass(slots=True)
class VerifiedClaim:
    claim: str
    evidence: list[str]
    confidence: float
    status: str


@dataclass(slots=True)
class VerificationResult:
    claims: list[VerifiedClaim]
    overall_verification_score: float
    summary: str


class VerificationService:
    def __init__(self) -> None:
        self.llm = get_chat_llm()

    def verify_from_evidence(self, evidence_text: str) -> VerificationResult:
        if not evidence_text.strip():
            return VerificationResult(
                claims=[],
                overall_verification_score=0.0,
                summary="No evidence was provided for verification.",
            )

        payload = self._verify_claims_once(evidence_text=evidence_text)
        claim_items = payload.get("claims", [])

        verified_claims: list[VerifiedClaim] = []
        for item in claim_items:
            if not isinstance(item, dict):
                continue

            claim = str(item.get("claim", "")).strip()
            evidence = item.get("evidence", [])
            confidence = self._clamp_score(item.get("confidence", 0.0))
            status = str(item.get("status", "not_supported")).strip()

            if not claim:
                continue
            if not isinstance(evidence, list):
                evidence = []

            verified_claims.append(
                VerifiedClaim(
                    claim=claim,
                    evidence=[str(x).strip() for x in evidence if str(x).strip()],
                    confidence=confidence,
                    status=status,
                )
            )

        overall_score = self._clamp_score(payload.get("overall_verification_score", 0.0))
        summary = str(payload.get("summary", "")).strip() or "Verification completed."

        return VerificationResult(
            claims=verified_claims,
            overall_verification_score=overall_score,
            summary=summary,
        )

    def _verify_claims_once(self, evidence_text: str) -> dict[str, Any]:
        system_prompt = (
            "You are a factual verification engine. "
            "In ONE pass, extract the key factual claims from the evidence text and verify them only against that evidence. "
            "Do not invent evidence. "
            "Return strict JSON with this shape: "
            "{"
            '"claims":['
            '{"claim":"...",'
            '"evidence":["...","..."],'
            '"confidence":0.0,'
            '"status":"supported|partially_supported|not_supported"}'
            "],"
            '"overall_verification_score":0.0,'
            '"summary":"..."'
            "}. "
            "Keep claims concise and non-duplicative. "
            "Confidence and overall_verification_score must be numbers from 0 to 1."
        )

        human_prompt = f"""
Evidence Text:
{evidence_text[:24000]}
"""

        response = self.llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ]
        )

        return self._parse_json_with_fallback(response.content)

    def _parse_json_with_fallback(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()

        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json"):].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[len("```"):].strip()

        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        try:
            return json.loads(cleaned)
        except JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = cleaned[start:end + 1]
                try:
                    return json.loads(candidate)
                except JSONDecodeError:
                    pass

        return {
            "claims": [],
            "overall_verification_score": 0.0,
            "summary": "Verification output could not be parsed as valid JSON.",
        }

    def _clamp_score(self, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 0.0
        return max(0.0, min(1.0, numeric))