"""Run ONE (task, configuration) pair through the real tool registry.

A fixed pipeline per strategy, not a free-running agent: the *configuration* (model,
strategy, budgets) is what varies between runs, and the sequence of tool calls it
produces is recorded so trajectory quality can be scored. ``run_case`` never raises:
every failure becomes a ``CaseOutput`` with ``status="failed"`` and the partial
trajectory/usage kept, so a failed attempt is still inspectable.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.agentic.common import ResearchMode, ToolName
from backend.bench.frozen import FrozenWorld
from backend.bench.models import AgentConfig, TaskDefinition
from backend.core.logging import get_logger
from backend.core.observability import append_trace

logger = get_logger(__name__)

MAX_EVIDENCE_PASSAGE_CHARS = 1500


@dataclass
class CaseOutput:
    status: str = "completed"                       # completed | failed
    error_type: Optional[str] = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)   # doc_ids in the order retrieval ranked them
    scraped: list[str] = field(default_factory=list)     # doc_ids whose text was actually fetched
    calls: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    assessment: Optional[str] = None
    budget_exhausted: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    raw_llm: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status, "error_type": self.error_type, "claims": self.claims, "retrieved": self.retrieved,
            "scraped": self.scraped, "calls": self.calls, "rounds": self.rounds, "assessment": self.assessment,
            "budget_exhausted": self.budget_exhausted, "usage": self.usage, "latency": self.latency,
        }


def case_from_json(d: dict[str, Any]) -> CaseOutput:
    return CaseOutput(
        status=d.get("status", "completed"), error_type=d.get("error_type"), claims=list(d.get("claims", [])),
        retrieved=list(d.get("retrieved", [])), scraped=list(d.get("scraped", [])), calls=list(d.get("calls", [])),
        rounds=int(d.get("rounds", 0)), assessment=d.get("assessment"), budget_exhausted=bool(d.get("budget_exhausted")),
        usage=dict(d.get("usage", {})), latency=dict(d.get("latency", {})),
    )


class _Done(Exception):
    """Normal early exit (abstention)."""


class _Abort(Exception):
    """A stage failed in a way that makes the case untrustworthy."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _code(result: Any) -> str:
    code = getattr(getattr(result, "error", None), "code", None)
    return str(getattr(code, "value", code)) if code is not None else "unknown"


def render_evidence(scraped_docs: list[Any], chars: int) -> str:
    """Same layout the M7 challenge engine hands to ``verify_claim``."""
    return "\n\n---\n\n".join(
        "\n".join([
            f"Source Title: {sc.title}", f"URL: {sc.url}", f"Domain: {sc.domain}",
            f"Publish Date: {sc.publish_date or 'Unknown'}", "Evidence Source: scraped_page",
            f"Content: {sc.content[:chars]}",
        ]) for sc in scraped_docs
    )


def claims_prompt(task: TaskDefinition) -> str:
    return "\n".join(f"{i}. {c.text}" for i, c in enumerate(task.claims, start=1))


def run_case(task: TaskDefinition, config: AgentConfig, llm: Any, *, run_key: str = "bench", user_id: str = "bench") -> CaseOutput:
    world = FrozenWorld(task, llm)
    out = CaseOutput()
    state: dict[str, Any] = {"research_trace": [], "session_id": run_key, "authenticated_user_id": user_id}
    started_all = time.perf_counter()
    budget = min(config.max_tool_calls, task.trajectory.max_tool_calls)
    registry = world.registry()
    scraped: dict[str, Any] = {}          # doc_id -> ScrapedContent, insertion-ordered
    seen_urls: list[str] = []

    def trace(action: str, status: str, t0: float = 0.0, error: Any = None, **meta: Any) -> None:
        ms = int((time.perf_counter() - t0) * 1000) if t0 else 0
        state["research_trace"] = append_trace(state, component="atlasbench", action=action, status=status,
                                               duration_ms=ms, metadata=meta, error=error)

    def remaining() -> int:
        return budget - len(out.calls)

    def call(tool: ToolName, payload: dict[str, Any], key: str, **meta: Any):
        t0 = time.perf_counter()
        res = registry.execute(tool, payload)
        out.calls.append({"seq": len(out.calls) + 1, "tool": tool.value, "ok": bool(res.success), "key": key,
                          "duration_ms": res.duration_ms, "error_code": None if res.success else _code(res), **meta})
        trace(tool.value, "succeeded" if res.success else "failed", t0, tool_error=None if res.success else _code(res), **meta)
        return res

    def scrape(sources: list[Any], limit: int) -> int:
        got = 0
        for s in sources:
            if got >= limit:
                break
            if remaining() < 2:   # always keep one call for verification
                out.budget_exhausted = True
                break
            url = str(s.url)
            doc = task.doc_by_url(url)
            res = call(ToolName.SCRAPE_SOURCE, {"url": url, "title": (s.title or "")[:300], "source_id": s.source_id[:120]}, key=url,
                       doc_id=doc.doc_id if doc else None)
            if not res.success or not res.output.success or res.output.scraped is None or not res.output.scraped.content.strip():
                continue
            if doc is not None and doc.doc_id not in scraped:
                scraped[doc.doc_id] = res.output.scraped
                out.scraped.append(doc.doc_id)
                got += 1
        return got

    def verify():
        evidence = render_evidence(list(scraped.values()), config.evidence_chars_per_source)
        digest = hashlib.sha256(evidence.encode()).hexdigest()[:16]
        res = call(ToolName.VERIFY_CLAIM, {"claim": claims_prompt(task), "evidence_text": evidence}, key=digest,
                   docs=len(scraped))
        if not res.success:
            raise _Abort("verification_failed")
        return res.output

    def search(tool: ToolName, payload: dict[str, Any], key: str):
        res = call(tool, payload, key=key)
        if not res.success:
            raise _Abort("search_failed")
        sources = res.output.sources if tool is ToolName.SEARCH_WEB else res.output.new_sources
        for s in sources:
            d = task.doc_by_url(str(s.url))
            if d is not None and d.doc_id not in out.retrieved:
                out.retrieved.append(d.doc_id)
            seen_urls.append(str(s.url))
        return sources

    try:
        if config.strategy == "full_corpus":
            sources = [_as_ranked(d) for d in task.corpus]
            budget_scrapes = min(config.max_scrapes, max(0, remaining() - 1))
            scrape(sources, budget_scrapes)
        else:
            sources = search(ToolName.SEARCH_WEB, {"query": task.question, "max_results": config.top_k,
                                                   "research_mode": ResearchMode.INVESTIGATE.value}, key=task.question)
            scrape(sources, min(config.max_scrapes, max(0, remaining() - 1)))   # keep one call for verify
        if not scraped:
            if config.strategy != "full_corpus" and not sources:
                # Search found nothing relevant. Stopping here (no verifier call, no claims) is the
                # honest outcome — scoring treats it as an abstention, not as a crash.
                out.assessment = "no_evidence"
                trace("abstain", "succeeded", reason="no_relevant_sources")
                raise _Done()
            raise _Abort("no_evidence_retrieved")

        result = verify()
        out.rounds = 1
        if config.strategy == "iterative":
            while (out.rounds < config.max_rounds and result.assessment is not None
                   and result.assessment.status != "supported" and result.evidence_gaps and remaining() >= 3):
                gap = result.evidence_gaps[0]
                more = search(ToolName.REQUEST_MORE_EVIDENCE, {
                    "question": task.question[:300], "gap_description": gap.missing_evidence[:300],
                    "exclude_urls": list(dict.fromkeys(seen_urls))[:100], "max_results": config.top_k,
                    "research_mode": ResearchMode.INVESTIGATE.value}, key=gap.missing_evidence[:300])
                before = len(scraped)
                scrape(more, max(0, min(config.max_scrapes - len(scraped), remaining() - 1)))
                if len(scraped) == before:
                    break
                result = verify()
                out.rounds += 1

        out.assessment = result.assessment.status if result.assessment else None
        out.claims = [
            {"claim_text": c.claim_text, "status": str(c.status), "confidence": float(c.confidence),
             "evidence": [{"url": str(e.url), "stance": e.stance, "strength": float(e.evidence_strength),
                           "title": e.title, "evidence_text": (e.evidence_text or "")[:MAX_EVIDENCE_PASSAGE_CHARS]}
                          for e in c.evidence]}
            for c in result.verification.claims
        ]
    except _Done:
        pass
    except _Abort as exc:
        out.status, out.error_type = "failed", exc.error_type
        trace("case", "failed", error=exc.error_type)
    except Exception as exc:  # defensive: the pipeline must never raise out of a case
        logger.warning("AtlasBench case crashed", extra={"error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)})
        out.status, out.error_type = "failed", type(exc).__name__
        trace("case", "failed", error=exc)

    usage = world.meter.summary()
    out.usage = usage
    tool_ms = sum(c["duration_ms"] for c in out.calls)
    out.latency = {"total_ms": int((time.perf_counter() - started_all) * 1000), "llm_ms": usage["llm_latency_ms"], "tool_ms": tool_ms}
    out.raw_llm = list(world.meter.raw_responses)
    out.trace = state["research_trace"]
    return out


def _as_ranked(doc: Any):
    from backend.bench.frozen import _domain, _publish, doc_source_id
    from backend.models.domain_models import RankedSource

    return RankedSource(source_id=doc_source_id(doc), title=doc.title, url=doc.url, domain=_domain(doc.url),
                        snippet=doc.content[:200], publish_date=_publish(doc))
