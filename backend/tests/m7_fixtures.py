"""M7 test harness: fake *services* behind the REAL ToolRegistry.

Only the external boundaries (web search, page scraping, the LLM verifier) are
faked; request validation, error containment, URL exclusion, provenance
filtering, persistence and classification are all the real code.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from backend.agentic.tools.builtin import ToolDependencies, build_default_registry
from backend.challenges.engine import ChallengeEngine
from backend.challenges.service import ChallengeService
from backend.models.domain_models import (
    ClaimEvidence, RankedSource, ScrapedContent, VerificationSummary, VerifiedClaim,
)
from backend.tests.m6_fixtures import (  # noqa: F401  (re-exported fixtures + helpers)
    NOW, S1, S2, S3, T1, T2, T3, _fake_embeddings, claim, evolving_scenario, investigate, make_user, repository, src, svc,
)

REPUTATION = {"a.example.com": 0.9, "c.example.net": 0.8, "b.example.org": 0.2, "lowrep.example.net": 0.2}


def page(url: str, *, content: str = "Page body text.", published: str | None = "2026-08-01", title: str = "Page") -> dict:
    return {"url": url, "content": content, "published": published, "title": title}


class FakeWorld:
    """Scriptable web: pages that exist, what the verifier concludes about each."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.verdicts: dict[str, tuple[str, float]] = {}   # url -> (stance, strength)
        self.invented: list[tuple[str, str, float]] = []   # (url, stance, strength): evidence citing never-retrieved URLs
        self.search_fail = 0       # fail the next N searches
        self.scrape_fail = False
        self.verify_fail = 0       # fail the next N verifications
        self.unreachable: set[str] = set()
        self.calls = {"search": 0, "scrape": 0, "verify": 0}
        self.queries: list[str] = []
        outer = self

        class Retrieval:
            def retrieve_and_rank(self, query, top_k, research_mode):
                outer.calls["search"] += 1
                outer.queries.append(query)
                if outer.search_fail > 0:
                    outer.search_fail -= 1
                    raise RuntimeError("search backend down")
                ranked = [RankedSource(title=p["title"], url=u, domain=u.split("/")[2], snippet="s", rank_position=i + 1,
                                       publish_date=date.fromisoformat(p["published"]) if p["published"] else None)
                          for i, (u, p) in enumerate(outer.pages.items())]
                return [], ranked[:top_k]

        class Scraping:
            async def scrape_sources(self, sources):
                outer.calls["scrape"] += 1
                if outer.scrape_fail:
                    raise RuntimeError("scraper exploded")
                out = []
                for s in sources:
                    p = outer.pages.get(str(s.url))
                    ok = p is not None and str(s.url) not in outer.unreachable
                    out.append(ScrapedContent(
                        source_id=s.source_id, url=str(s.url), title=p["title"] if p else "", domain=s.domain,
                        publish_date=date.fromisoformat(p["published"]) if p and p["published"] else None,
                        content=p["content"] if ok else "", content_hash=f"hash-{abs(hash(str(s.url))) % 10**8}" if ok else "",
                        success=ok, error_message=None if ok else "unreachable"))
                return out

        class Verifier:
            def verify(self, report_markdown, scraped_research_text):
                outer.calls["verify"] += 1
                if outer.verify_fail > 0:
                    outer.verify_fail -= 1
                    raise RuntimeError("llm unavailable")
                # the real verify_claim tool collapses newlines, so match tokens, not lines
                shown = re.findall(r"URL: (\S+) Domain:", scraped_research_text)
                evidence = [
                    ClaimEvidence(source_id="", url=u, title="t", evidence_text=f"Evidence from {u}", stance=outer.verdicts[u][0],
                                  evidence_strength=outer.verdicts[u][1])
                    for u in shown if u in outer.verdicts
                ] + [ClaimEvidence(source_id="", url=u, title="fake", evidence_text="Invented passage", stance=st, evidence_strength=sg)
                     for (u, st, sg) in outer.invented]
                status = "verified" if evidence and all(e.stance == "support" for e in evidence) else "conflicting" if evidence else "insufficient_evidence"
                return VerificationSummary(overall_verification_score=50, verdict="ok", claims=[
                    VerifiedClaim(claim_text=report_markdown, confidence=0.7, status=status, evidence=evidence)])

        self.retrieval, self.scraping, self.verifier = Retrieval(), Scraping(), Verifier()

    def add(self, url: str, stance: str | None = None, strength: float = 0.8, **kw) -> "FakeWorld":
        self.pages[url] = page(url, **kw)
        if stance:
            self.verdicts[url] = (stance, strength)
        return self

    def registry(self):
        return build_default_registry(ToolDependencies(
            retrieval_service=lambda: self.retrieval, scraping_service=lambda: self.scraping,
            verification_agent=lambda: self.verifier, vector_memory=lambda: None))


@pytest.fixture()
def world():
    return FakeWorld()


@pytest.fixture()
def engine(world):
    return ChallengeEngine(world.registry(), clock=lambda: NOW, reputation=lambda d: REPUTATION.get(d, 0.5))


@pytest.fixture()
def cs(repository, engine):
    return ChallengeService(repository, engine=engine, clock=lambda: NOW)


def solar_claim(svc, user):
    """(claim_id, investigation_id) of 'Solar capacity grew in Europe' as seen in the latest investigation."""
    c = next(c for c in svc.list_claims(user, query="solar", limit=5)["items"])
    return c["claim_id"], c["investigation"]["report_id"]
