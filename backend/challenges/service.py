"""Challenge service: ownership-checked create, state-machine execute, and views.

Ownership: every operation takes the authenticated ``user_id``. A challenge,
claim or investigation that does not exist *or belongs to another user* raises
the same not-found error, so existence is never leaked.

History is never overwritten: a challenge is a separate persisted event and the
claim's M5 observations/evidence are untouched. The "updated interpretation" is
derived (``classify.interpret``) from the original observation + completed
challenges.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from backend.challenges.classify import BaseEvidence, interpret
from backend.challenges.engine import ChallengeEngine, ClaimContext
from backend.challenges.models import CHALLENGE_TYPES, MAX_ATTEMPTS, MIN_CLAIM_CHARS
from backend.challenges.repository import ChallengeConflict, ChallengeNotFound, ChallengeRepository
from backend.core.logging import get_logger
from backend.core.observability import serialize_public_trace
from backend.database import ResearchRepository
from backend.knowledge.map_service import KnowledgeMapService, NotFoundError

logger = get_logger(__name__)


class ChallengeStateError(Exception):
    """The claim's investigation knowledge is not in a state that can be challenged."""


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class ChallengeService:
    def __init__(
        self,
        repository: ResearchRepository,
        *,
        engine: Optional[ChallengeEngine] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.repository = repository
        self.repo = ChallengeRepository(repository.db)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.engine = engine or ChallengeEngine(clock=self._clock)

    # ── create ───────────────────────────────────────────────────────────────
    def create(self, user_id: str, investigation_id: str, claim_id: str, challenge_type: str) -> tuple[dict, bool]:
        """Create (or return the already-active) challenge. Returns ``(view, created)``."""
        if challenge_type not in CHALLENGE_TYPES:
            raise ValueError(f"challenge_type must be one of: {', '.join(CHALLENGE_TYPES)}")
        km = KnowledgeMapService(self.repository, clock=self._clock)
        try:
            claim = km.get_claim(user_id, claim_id, report_id=investigation_id)  # NotFoundError if not the caller's
        except NotFoundError:
            raise ChallengeNotFound("claim not found in that investigation") from None
        if claim["graph_state"] not in ("complete", "partial"):
            raise ChallengeStateError(
                f"Knowledge for this investigation is '{claim['graph_state']}', so its claims cannot be challenged until it is rebuilt.")
        if len(claim["text"]) < MIN_CLAIM_CHARS:
            raise ValueError(f"The claim is too short to challenge (minimum {MIN_CLAIM_CHARS} characters).")

        evidence = [
            {"evidence_id": e["evidence_id"], "url": e["source"]["url"], "domain": e["source"]["domain"],
             "title": e["source"].get("title", ""), "publish_date": e["source"].get("publish_date"),
             "stance": e["stance"], "strength": e["strength"]}
            for group in claim["evidence"].values() for e in group
        ]
        base = {
            "status": claim["status"], "confidence": claim["confidence"], "observed_at": claim["observed_at"],
            "graph_state": claim["graph_state"], "topic": claim["investigation"]["topic"], "evidence": evidence,
        }
        row, created = self.repo.create(user_id, investigation_id, claim_id, challenge_type, claim["text"], base, self._clock())
        return self._view(row), created

    # ── execute ──────────────────────────────────────────────────────────────
    def execute(self, user_id: str, challenge_id: str) -> tuple[dict, bool]:
        """Run the challenge. Returns ``(view, ran)``; ``ran`` is False when it had
        already completed (execution is idempotent — the stored result is returned)."""
        self.repo.get(user_id, challenge_id)  # ownership / existence first
        try:
            row, attempt = self.repo.begin_attempt(user_id, challenge_id, self._clock())
        except ChallengeConflict as exc:
            if str(exc) == "completed":
                return self._view(self.repo.get(user_id, challenge_id)), False
            raise

        base = json.loads(row["base_json"] or "{}")
        ctx = ClaimContext(
            challenge_id=challenge_id, user_id=user_id, claim_text=row["claim_text"],
            base_evidence=[BaseEvidence(url=e["url"], domain=e.get("domain") or "", stance=e.get("stance") or "support",
                                        strength=float(e.get("strength") or 0.0), publish_date=_parse_date(e.get("publish_date")))
                           for e in base.get("evidence", [])],
            original_urls=[e["url"] for e in base.get("evidence", []) if e.get("url")],
        )
        try:
            outcome = self.engine.run(ctx, row["challenge_type"])
            self.repo.complete(
                user_id, challenge_id, attempt, result=outcome.classification.result,
                result_json={
                    "rationale": outcome.classification.rationale, "factors": outcome.classification.factors,
                    "queries": outcome.queries, "verification": outcome.verification,
                    "dropped_unverified_evidence": outcome.dropped_unverified_evidence,
                    "sources_retrieved": len(outcome.sources), "as_of": self._clock().isoformat(),
                },
                sources=outcome.sources, evidence=outcome.evidence, trace=outcome.trace, now=self._clock(),
            )
        except ChallengeConflict:
            raise  # lost the lease to another attempt: not ours to mark failed
        except Exception as exc:
            logger.warning("Challenge attempt failed", extra={"challenge_id": challenge_id, "error": str(exc), "error_type": type(exc).__name__})
            self.repo.fail(
                user_id, challenge_id, attempt, error_type=getattr(exc, "error_type", type(exc).__name__),
                message=str(exc), trace=getattr(exc, "trace", []), now=self._clock())
        return self._view(self.repo.get(user_id, challenge_id)), True

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, user_id: str, challenge_id: str) -> dict:
        return self._view(self.repo.get(user_id, challenge_id))

    def list(self, user_id: str, **filters: Any) -> dict:
        rows, total = self.repo.list(user_id, **filters)
        return {"items": [self._view(r) for r in rows], "total": total,
                "limit": max(1, min(int(filters.get("limit", 25)), 100)), "offset": max(0, int(filters.get("offset", 0)))}

    def claim_interpretation(self, user_id: str, claim_id: str, base_status: str, base_confidence: float) -> dict:
        return interpret(base_status, base_confidence, self.repo.list_for_claim(user_id, claim_id))

    def inspect(self, user_id: str, challenge_id: str) -> dict:
        row = self.repo.get(user_id, challenge_id)
        view = self._view(row)
        result = json.loads(row["result_json"] or "{}")
        base = json.loads(row["base_json"] or "{}")
        sources = self.repo.sources(user_id, challenge_id)
        evidence = [self._evidence_view(e) for e in self.repo.evidence(user_id, challenge_id)]
        attempts = self.repo.attempts(user_id, challenge_id)
        last_trace = []
        for a in reversed(attempts):
            if a.get("trace_json"):
                last_trace = serialize_public_trace(json.loads(a["trace_json"]))
                break
        interpretation = self.claim_interpretation(user_id, row["claim_id"], base.get("status", ""), base.get("confidence", 0.0))
        return {
            **view,
            "queries": result.get("queries", []),
            "classification": {"rationale": result.get("rationale"), "factors": result.get("factors", {})},
            "verification": result.get("verification"),
            "dropped_unverified_evidence": result.get("dropped_unverified_evidence", 0),
            "sources": [
                {"source_id": s["source_id"], "url": s["url"], "domain": s["domain"], "title": s["title"], "publish_date": s["publish_date"],
                 "scraped": bool(s["scraped"]), "independent": bool(s["independent"]), "content_hash": s["content_hash"], "retrieved_at": s["retrieved_at"]}
                for s in sources
            ],
            "evidence": evidence,
            "attempts": [{"attempt_no": a["attempt_no"], "status": a["status"], "started_at": a["started_at"],
                          "finished_at": a["finished_at"], "error_type": a["error_type"]} for a in attempts],
            "trace": last_trace,
            "interpretation": interpretation,
            "trail": self._trail(row, base, result, evidence, interpretation),
        }

    # ── views ────────────────────────────────────────────────────────────────
    def _view(self, row: dict) -> dict:
        base = json.loads(row["base_json"] or "{}")
        result = json.loads(row["result_json"] or "{}")
        return {
            "challenge_id": row["challenge_id"], "investigation_id": row["investigation_id"], "claim_id": row["claim_id"],
            "claim_text": row["claim_text"], "challenge_type": row["challenge_type"], "status": row["status"], "result": row["result"],
            "attempt_count": row["attempt_count"], "max_attempts": MAX_ATTEMPTS,
            "retryable": row["status"] == "failed" and row["attempt_count"] < MAX_ATTEMPTS,
            "error_type": row["error_type"],  # class name only; the message stays server-side
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "started_at": row["started_at"], "completed_at": row["completed_at"],
            "base": {"status": base.get("status"), "confidence": base.get("confidence"), "graph_state": base.get("graph_state")},
            "rationale": result.get("rationale"), "sources_retrieved": result.get("sources_retrieved", 0),
        }

    @staticmethod
    def _evidence_view(e: dict) -> dict:
        role = {"contradict": "counter", "support": "supporting"}.get(e["stance"], "qualifying")
        return {
            "evidence_id": e["evidence_id"], "passage": e["passage_text"], "stance": e["stance"], "role": role, "strength": e["strength"],
            "source": {"source_id": e["source_id"], "url": e["url"], "domain": e["domain"], "title": e["title"],
                       "publish_date": e["publish_date"], "independent": bool(e["independent"]),
                       "content_hash": e["content_hash"], "retrieved_at": e["retrieved_at"]},
            "provenance": {"retrieved_source": True},
        }

    @staticmethod
    def _trail(row: dict, base: dict, result: dict, evidence: list[dict], interpretation: dict) -> list[dict]:
        """Claim → Evidence → Challenge → Counter-evidence → Result → Updated interpretation."""
        return [
            {"step": "claim", "text": row["claim_text"], "status": base.get("status"), "confidence": base.get("confidence")},
            {"step": "evidence", "items": [{"url": e.get("url"), "domain": e.get("domain"), "stance": e.get("stance"),
                                            "strength": e.get("strength")} for e in base.get("evidence", [])]},
            {"step": "challenge", "challenge_type": row["challenge_type"], "status": row["status"],
             "queries": [f"{q['question']} — {q['focus']}" for q in result.get("queries", [])]},
            {"step": "counter_evidence", "items": [
                {"evidence_id": e["evidence_id"], "role": e["role"], "stance": e["stance"], "strength": e["strength"],
                 "url": e["source"]["url"], "independent": e["source"]["independent"]} for e in evidence]},
            {"step": "result", "value": row["result"], "rationale": result.get("rationale")},
            {"step": "updated_interpretation", **{k: interpretation[k] for k in ("label", "summary", "adjusted_confidence")}},
        ]
