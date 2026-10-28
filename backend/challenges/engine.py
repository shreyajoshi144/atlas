"""Challenge engine: claim → challenge queries → retrieve → verify → classify.

A FIXED pipeline, not an agent. Every external action goes through the closed
``ToolRegistry`` (``request_more_evidence`` → ``scrape_source`` → ``verify_claim``)
so inputs are validated, URLs are SSRF-guarded by the scraper, page content is
wrapped as untrusted by the verification agent, and failures are contained.

Provenance rule: evidence is kept ONLY if its URL is a page this run actually
retrieved and scraped. Anything else (e.g. a URL an LLM invented) is dropped and
counted — it can never become a challenge result.

Independence: the claim's own source URLs are excluded from the search and any
that slip through are filtered again by canonical URL; evidence from a domain the
claim already relied on is flagged ``independent = False``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from backend.agentic.common import ToolName
from backend.challenges.classify import BaseEvidence, Classification, NewEvidence, classify
from backend.challenges.models import EVIDENCE_CHARS_PER_SOURCE, MAX_SCRAPES, RESULTS_PER_QUERY
from backend.challenges.queries import build_queries
from backend.core.logging import get_logger
from backend.core.observability import append_trace
from backend.knowledge import ids
from backend.knowledge.models import STANCES

logger = get_logger(__name__)
_MAX_PASSAGE = 2000


class ChallengeToolFailure(RuntimeError):
    """A pipeline stage failed in a way that makes the run untrustworthy (retryable).

    Carries the partial trace so the failed attempt is still inspectable.
    """

    def __init__(self, message: str, trace: list[dict], cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.trace = trace
        self.cause = cause

    @property
    def error_type(self) -> str:
        return type(self.cause).__name__ if self.cause is not None else type(self).__name__


@dataclass
class ClaimContext:
    challenge_id: str
    user_id: str
    claim_text: str
    base_evidence: list[BaseEvidence] = field(default_factory=list)
    original_urls: list[str] = field(default_factory=list)


@dataclass
class EngineOutcome:
    classification: Classification
    queries: list[dict]
    sources: list[dict]
    evidence: list[dict]
    dropped_unverified_evidence: int
    verification: Optional[dict]
    trace: list[dict]


def _code(result: Any) -> str:
    """Tool error code as text (ToolError stores it as a plain string, not an Enum)."""
    code = getattr(getattr(result, "error", None), "code", None)
    return str(getattr(code, "value", code)) if code is not None else "unknown"


def _canon(url: str) -> Optional[str]:
    try:
        return ids.canonicalize_url(url)
    except ValueError:
        return None


def _default_registry() -> Any:
    from backend.agentic.tools.builtin import get_tool_registry

    return get_tool_registry()


def _default_reputation() -> Callable[[str], float]:
    from backend.services.ranking_service import RankingService

    return RankingService()._domain_reputation


class ChallengeEngine:
    def __init__(
        self,
        registry: Any = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        reputation: Optional[Callable[[str], float]] = None,
        results_per_query: int = RESULTS_PER_QUERY,
        max_scrapes: int = MAX_SCRAPES,
    ) -> None:
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._reputation = reputation
        self.results_per_query = results_per_query
        self.max_scrapes = max_scrapes

    def run(self, ctx: ClaimContext, challenge_type: str) -> EngineOutcome:
        state: dict[str, Any] = {"research_trace": [], "session_id": ctx.challenge_id, "authenticated_user_id": ctx.user_id}

        def trace(action: str, status: str, started: float = 0.0, error: Any = None, **meta: Any) -> None:
            ms = int((time.perf_counter() - started) * 1000) if started else 0
            state["research_trace"] = append_trace(
                state, component="challenge", action=action, status=status, duration_ms=ms, metadata=meta, error=error)

        try:
            return self._run(ctx, challenge_type, trace, state)
        except ChallengeToolFailure:
            raise
        except Exception as exc:  # any unexpected error: fail the attempt, keep the trace
            trace("run", "failed", error=exc)
            raise ChallengeToolFailure(f"{type(exc).__name__}: {exc}", state["research_trace"], cause=exc) from exc

    # ── pipeline ─────────────────────────────────────────────────────────────
    def _run(self, ctx: ClaimContext, challenge_type: str, trace: Callable[..., None], state: dict) -> EngineOutcome:
        registry = self._registry or _default_registry()
        reputation = self._reputation or _default_reputation()
        now = self._clock()
        queries = build_queries(challenge_type, ctx.claim_text, year=now.year)
        trace("plan", "succeeded", challenge_type=challenge_type, query_count=len(queries))

        original_canon = {c for c in (_canon(u) for u in ctx.original_urls) if c}
        original_domains = {b.domain for b in ctx.base_evidence if b.domain}

        # 1) retrieve opposing / independent / new sources ----------------------
        found: dict[str, dict] = {}
        query_log: list[dict] = []
        failures = 0
        for qi, q in enumerate(queries):
            started = time.perf_counter()
            exclude = list(dict.fromkeys(ctx.original_urls + [str(i["ranked"].url) for i in found.values()]))[:100]
            res = registry.execute(ToolName.REQUEST_MORE_EVIDENCE, {
                "question": q["question"], "gap_description": q["focus"], "exclude_urls": exclude,
                "max_results": self.results_per_query, "research_mode": "investigate",
            })
            if not res.success:
                failures += 1
                query_log.append({**q, "ok": False, "returned": 0, "new": 0})
                trace("search", "failed", started, query_index=qi, tool_error=_code(res))
                continue
            returned = res.output.new_sources
            new = filtered_original = 0
            for s in returned:
                canon = _canon(str(s.url))
                if canon is None:
                    continue
                if canon in original_canon:
                    filtered_original += 1
                    continue
                if canon not in found:
                    found[canon] = {"ranked": s, "query_index": qi}
                    new += 1
            query_log.append({**q, "ok": True, "returned": len(returned), "new": new, "filtered_original": filtered_original})
            trace("search", "succeeded", started, query_index=qi, returned=len(returned), new=new, filtered_original=filtered_original)
        if queries and failures == len(queries):
            raise ChallengeToolFailure("All challenge searches failed.", state["research_trace"])

        # 2) retrieve page text (SSRF-guarded scraper) --------------------------
        scraped: dict[str, Any] = {}
        to_scrape = list(found.items())[: self.max_scrapes]
        scrape_errors = 0
        for canon, item in to_scrape:
            s, started = item["ranked"], time.perf_counter()
            res = registry.execute(ToolName.SCRAPE_SOURCE, {"url": str(s.url), "title": (s.title or s.domain)[:300], "source_id": s.source_id[:120]})
            if not res.success:
                scrape_errors += 1
                trace("scrape", "failed", started, domain=s.domain, tool_error=_code(res))
                continue
            out = res.output
            if out.success and out.scraped is not None and out.scraped.content.strip():
                scraped[canon] = out.scraped
                trace("scrape", "succeeded", started, domain=s.domain, chars=len(out.scraped.content))
            else:
                trace("scrape", "skipped", started, domain=s.domain, reason="unreachable_or_empty")
        if to_scrape and scrape_errors == len(to_scrape):
            raise ChallengeToolFailure("Every source retrieval failed.", state["research_trace"])

        # 3) verify the claim against ONLY what was actually retrieved ----------
        evidence: dict[str, dict] = {}
        dropped = 0
        verification: Optional[dict] = None
        if scraped:
            started = time.perf_counter()
            corpus = "\n\n---\n\n".join(
                "\n".join([
                    f"Source Title: {sc.title}", f"URL: {sc.url}", f"Domain: {sc.domain}",
                    f"Publish Date: {sc.publish_date or 'Unknown'}", "Evidence Source: scraped_page",
                    f"Content: {sc.content[:EVIDENCE_CHARS_PER_SOURCE]}",
                ]) for sc in scraped.values()
            )
            res = registry.execute(ToolName.VERIFY_CLAIM, {"claim": ctx.claim_text, "evidence_text": corpus})
            if not res.success:
                trace("verify", "failed", started, tool_error=_code(res))
                raise ChallengeToolFailure("Verification of the challenge evidence failed.", state["research_trace"])
            out = res.output
            verification = {
                "assessment": out.assessment.status if out.assessment else None,
                "claim_statuses": [str(c.status) for c in out.verification.claims],
            }
            for vc in out.verification.claims:
                for ev in vc.evidence:
                    canon = _canon(str(ev.url))
                    src, passage = scraped.get(canon or ""), (ev.evidence_text or "").strip()
                    if src is None or not passage:
                        dropped += 1  # provenance: not a page this run retrieved
                        continue
                    source_id = ids.source_id(ctx.user_id, str(src.url))
                    eid = ids.evidence_id(ctx.user_id, source_id, passage)
                    stance = ev.stance if ev.stance in STANCES else "mixed"
                    strength = min(max(float(ev.evidence_strength or 0.0), 0.0), 1.0)
                    if eid not in evidence or strength > evidence[eid]["strength"]:
                        evidence[eid] = {
                            "evidence_id": eid, "source_id": source_id, "passage_text": passage[:_MAX_PASSAGE],
                            "stance": stance, "strength": strength, "canonical_url": canon,
                        }
            trace("verify", "succeeded", started, claims=len(out.verification.claims), evidence_kept=len(evidence), evidence_dropped=dropped)

        # 4) provenance-complete records ----------------------------------------
        sources: list[dict] = []
        for canon, item in found.items():
            s, sc = item["ranked"], scraped.get(canon)
            published = (sc.publish_date if sc is not None else None) or s.publish_date
            sources.append({
                "source_id": ids.source_id(ctx.user_id, str(s.url)), "url": str(s.url), "canonical_url": canon,
                "domain": s.domain, "title": (s.title or "")[:300], "publish_date": published.isoformat() if published else None,
                "content_hash": sc.content_hash if sc is not None else None, "scraped": 1 if sc is not None else 0,
                "independent": 0 if s.domain in original_domains else 1, "query_index": item["query_index"],
                "retrieved_at": now.isoformat(),
            })
        by_source = {s["source_id"]: s for s in sources}
        for e in evidence.values():
            src = by_source[e["source_id"]]
            e["independent"] = bool(src["independent"])
            e["url"], e["domain"] = src["url"], src["domain"]
            e["publish_date"] = src["publish_date"]

        # 5) classify ------------------------------------------------------------
        new_evidence = [
            NewEvidence(source_id=e["source_id"], url=e["url"], domain=e["domain"], stance=e["stance"], strength=e["strength"],
                        independent=e["independent"], publish_date=date.fromisoformat(e["publish_date"]) if e["publish_date"] else None)
            for e in evidence.values()
        ]
        classification = classify(
            challenge_type, base_evidence=ctx.base_evidence, new_evidence=new_evidence, retrieved_sources=len(found),
            scraped_sources=len(scraped), as_of=now.date(), reputation=reputation,
        )
        trace("classify", "succeeded", result=classification.result, new_evidence=len(new_evidence))
        return EngineOutcome(
            classification=classification, queries=query_log, sources=sources, evidence=sorted(evidence.values(), key=lambda e: e["evidence_id"]),
            dropped_unverified_evidence=dropped, verification=verification, trace=state["research_trace"],
        )
