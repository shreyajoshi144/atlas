"""Phase 1B — the five built-in tools.

Every handler is a thin adapter over an **existing** Atlas service; none
re-implements retrieval, scraping, memory search or verification:

  search_web                → RetrievalService.retrieve_and_rank
  scrape_source             → AsyncScrapingService.scrape_sources   (SSRF guard lives there)
  search_existing_evidence  → VectorMemoryService.search_similar + ResearchRepository.reports.get_report
  verify_claim              → VerificationAgent.verify
  request_more_evidence     → RetrievalService.retrieve_and_rank    (gap-targeted, de-duplicated)

Services are resolved lazily — importing this module loads no LLM, Chroma or
Tavily client — and, where the graph already has a cached singleton
(``backend.graph.nodes``), the same instance is reused.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urlparse

from backend.agentic.common import ToolName, compose_query
from backend.agentic.tools.base import ToolSpec
from backend.agentic.tools.registry import ToolRegistry
from backend.agentic.tools.schemas import (
    EvidenceGap,
    ExistingEvidenceHit,
    VerificationAssessment,
    RequestMoreEvidenceInput,
    RequestMoreEvidenceOutput,
    ScrapeSourceInput,
    ScrapeSourceOutput,
    SearchExistingEvidenceInput,
    SearchExistingEvidenceOutput,
    SearchWebInput,
    SearchWebOutput,
    VerifyClaimInput,
    VerifyClaimOutput,
)
from backend.core.logging import get_logger
from backend.models.domain_models import RankedSource

logger = get_logger(__name__)


# ── lazy default service providers ───────────────────────────────────────────
def _default_retrieval_service() -> Any:
    from backend.graph import nodes

    return nodes._retrieval_service()


def _default_scraping_service() -> Any:
    from backend.graph import nodes

    return nodes._scraping_service()


def _default_verification_agent() -> Any:
    from backend.graph import nodes

    return nodes._verification_agent()


def _default_vector_memory() -> Any:
    from backend.memory import VectorMemoryService

    return VectorMemoryService()


@dataclass(frozen=True)
class ToolDependencies:
    """Zero-argument providers, called only when a tool actually runs.
    Override any of them (e.g. in tests) to inject a different service."""

    retrieval_service: Callable[[], Any] = _default_retrieval_service
    scraping_service: Callable[[], Any] = _default_scraping_service
    verification_agent: Callable[[], Any] = _default_verification_agent
    vector_memory: Callable[[], Any] = _default_vector_memory


# ── helpers ──────────────────────────────────────────────────────────────────
def _domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _url_key(url: Any) -> str:
    # Same normalisation RetrievalService uses to de-duplicate results.
    return str(url).rstrip("/").lower()


def _run_coroutine_sync(coro: Any) -> Any:
    """Run a coroutine to completion from sync code. Uses a worker thread so it
    is safe whether or not the caller is already inside an event loop — the
    same approach ``scrape_node`` uses."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ── handler factories ────────────────────────────────────────────────────────
def _make_search_web(deps: ToolDependencies) -> Callable[[SearchWebInput], SearchWebOutput]:
    def handler(inp: SearchWebInput) -> SearchWebOutput:
        search_results, ranked = deps.retrieval_service().retrieve_and_rank(
            query=inp.query, top_k=inp.max_results, research_mode=inp.research_mode
        )
        return SearchWebOutput(
            query=inp.query,
            research_mode=inp.research_mode,
            raw_result_count=len(search_results),
            sources=ranked,
        )

    return handler


def _make_scrape_source(deps: ToolDependencies) -> Callable[[ScrapeSourceInput], ScrapeSourceOutput]:
    def handler(inp: ScrapeSourceInput) -> ScrapeSourceOutput:
        url = str(inp.url)
        domain = _domain_of(url)
        fields: dict[str, Any] = {"title": inp.title or domain, "url": url, "domain": domain}
        if inp.source_id:
            fields["source_id"] = inp.source_id
        source = RankedSource(**fields)

        results = _run_coroutine_sync(deps.scraping_service().scrape_sources([source]))
        scraped = next((r for r in results if r.source_id == source.source_id), None)
        if scraped is None:
            return ScrapeSourceOutput(
                source_id=source.source_id,
                success=False,
                error_message="The scraper returned no result for this URL.",
            )
        return ScrapeSourceOutput(
            source_id=source.source_id,
            success=bool(scraped.success),
            scraped=scraped,
            error_message=scraped.error_message,
        )

    return handler


def _make_search_existing_evidence(
    deps: ToolDependencies,
) -> Callable[[SearchExistingEvidenceInput], SearchExistingEvidenceOutput]:
    def handler(inp: SearchExistingEvidenceInput) -> SearchExistingEvidenceOutput:
        memory = deps.vector_memory()
        # user_id scopes the vector query itself (VectorMemoryService enforces it).
        matches = memory.search_similar(
            query=inp.query, user_id=inp.user_id, limit=inp.limit, topic=inp.topic
        )
        hits: list[ExistingEvidenceHit] = []
        for match in matches:
            # Re-check ownership at the SQL layer and fetch the stored summary,
            # mirroring the defence-in-depth the memory service itself applies.
            row = memory.repository.reports.get_report(report_id=match["report_id"], user_id=inp.user_id)
            if not row:
                continue
            hits.append(
                ExistingEvidenceHit(
                    report_id=match["report_id"],
                    topic=row.get("topic") or match.get("topic", ""),
                    summary=row.get("summary") or "",
                    similarity_score=min(max(float(match["similarity_score"]), 0.0), 1.0),
                )
            )
        return SearchExistingEvidenceOutput(query=inp.query, hits=hits)

    return handler


def _make_verify_claim(deps: ToolDependencies) -> Callable[[VerifyClaimInput], VerifyClaimOutput]:
    def handler(inp: VerifyClaimInput) -> VerifyClaimOutput:
        summary = deps.verification_agent().verify(
            report_markdown=inp.claim,
            scraped_research_text=inp.evidence_text,
        )
        gaps: list[EvidenceGap] = []
        statuses = {str(c.status) for c in summary.claims}
        if statuses and statuses.issubset({"verified"}):
            assessment = VerificationAssessment(
                status="supported",
                rationale="All verified claims are supported by the supplied evidence.",
            )
        elif "conflicting" in statuses:
            assessment = VerificationAssessment(
                status="conflicting_evidence",
                rationale="At least one required claim has materially conflicting evidence.",
            )
        elif "not_verified" in statuses:
            assessment = VerificationAssessment(
                status="unsupported_claim",
                rationale="At least one claim remains unsupported by the supplied evidence.",
            )
        else:
            assessment = VerificationAssessment(
                status="insufficient_evidence",
                rationale="The supplied evidence is insufficient to verify every required claim.",
            )

        for verified_claim in summary.claims:
            status = str(verified_claim.status)
            if status == "verified":
                continue
            if status == "conflicting":
                missing = "Independent evidence that resolves or explains the conflicting sources."
                reason = "The available sources disagree on the claim."
                preferred_source = ", ".join(str(e.url) for e in verified_claim.evidence[:3]) or None
            elif status == "not_verified":
                missing = "Direct, authoritative evidence supporting or refuting the claim."
                reason = "The claim could not be supported by the supplied evidence."
                preferred_source = None
            else:
                missing = "Additional independent evidence that directly supports the claim."
                reason = "The available evidence does not meet the required support threshold."
                preferred_source = ", ".join(str(e.url) for e in verified_claim.evidence[:3]) or None
            gaps.append(
                EvidenceGap(
                    claim=verified_claim.claim_text,
                    missing_evidence=missing,
                    reason=reason,
                    preferred_tool=ToolName.REQUEST_MORE_EVIDENCE.value,
                    preferred_source=preferred_source,
                )
            )
        return VerifyClaimOutput(
            claim=inp.claim,
            verification=summary,
            assessment=assessment,
            evidence_gaps=gaps,
        )

    return handler


def _make_request_more_evidence(
    deps: ToolDependencies,
) -> Callable[[RequestMoreEvidenceInput], RequestMoreEvidenceOutput]:
    def handler(inp: RequestMoreEvidenceInput) -> RequestMoreEvidenceOutput:
        query = compose_query(inp.question, inp.gap_description)
        seen = {_url_key(u) for u in inp.exclude_urls}
        # Over-fetch by the number of already-seen URLs so de-duplication does
        # not starve the result (retrieval itself caps at 20).
        top_k = min(20, inp.max_results + len(seen))
        _, ranked = deps.retrieval_service().retrieve_and_rank(
            query=query, top_k=top_k, research_mode=inp.research_mode
        )
        fresh = [s for s in ranked if _url_key(s.url) not in seen]
        return RequestMoreEvidenceOutput(
            query=query,
            research_mode=inp.research_mode,
            filtered_already_seen=len(ranked) - len(fresh),
            new_sources=fresh[: inp.max_results],
        )

    return handler


# ── registry construction ────────────────────────────────────────────────────
def build_default_registry(dependencies: ToolDependencies | None = None) -> ToolRegistry:
    deps = dependencies or ToolDependencies()
    return ToolRegistry(
        [
            ToolSpec(
                name=ToolName.SEARCH_WEB,
                description=(
                    "Search the web for a query and return ranked, de-duplicated sources "
                    "(title, URL, snippet, relevance and credibility scores). Does not fetch page content."
                ),
                input_model=SearchWebInput,
                output_model=SearchWebOutput,
                handler=_make_search_web(deps),
            ),
            ToolSpec(
                name=ToolName.SCRAPE_SOURCE,
                description=(
                    "Fetch and extract the main text of ONE http(s) URL through Atlas's SSRF-guarded scraper. "
                    "Returns the scraped content or a failure reason; never raises for blocked or unreachable pages."
                ),
                input_model=ScrapeSourceInput,
                output_model=ScrapeSourceOutput,
                handler=_make_scrape_source(deps),
            ),
            ToolSpec(
                name=ToolName.SEARCH_EXISTING_EVIDENCE,
                description=(
                    "Semantic search over the requesting user's previously saved research reports. "
                    "Requires the authenticated user_id and never returns another user's data."
                ),
                input_model=SearchExistingEvidenceInput,
                output_model=SearchExistingEvidenceOutput,
                handler=_make_search_existing_evidence(deps),
            ),
            ToolSpec(
                name=ToolName.VERIFY_CLAIM,
                description=(
                    "Verify a claim against a supplied evidence corpus with Atlas's verification agent. "
                    "Grounded only in the given evidence; returns the structured verification summary."
                ),
                input_model=VerifyClaimInput,
                output_model=VerifyClaimOutput,
                handler=_make_verify_claim(deps),
            ),
            ToolSpec(
                name=ToolName.REQUEST_MORE_EVIDENCE,
                description=(
                    "Run one targeted extra search for a stated evidence gap, excluding URLs already collected. "
                    "Returns only new ranked sources; it does not scrape them."
                ),
                input_model=RequestMoreEvidenceInput,
                output_model=RequestMoreEvidenceOutput,
                handler=_make_request_more_evidence(deps),
            ),
        ]
    )


@lru_cache(maxsize=1)
def get_tool_registry() -> ToolRegistry:
    """Process-wide registry wired to the real services (resolved lazily)."""
    return build_default_registry()
