"""The frozen world: the real M4 tool registry wired to ONE task's corpus.

Only the *boundaries* are swapped; everything an agent actually goes through is
production code:

* retrieval  → the M5 ``HybridRetriever`` (lexical BM25 stage) over the task corpus
* scraping   → returns the frozen document text; it never opens a socket, so there is
               no SSRF surface and no network non-determinism by construction
* verifying  → the real ``VerificationAgent`` driven by the configuration's model,
               wrapped in a ``MeteredLLM``
* the registry itself (input validation, error containment, typed outputs) is the real one

Documents are untrusted data exactly as on the live path: the verifier wraps the
evidence in ``wrap_untrusted_content`` so an injected instruction in a corpus
document is tested, not obeyed.
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from backend.agentic.tools.builtin import ToolDependencies, build_default_registry
from backend.bench.metering import MeteredLLM, UsageMeter
from backend.bench.models import BenchDoc, TaskDefinition
from backend.models.domain_models import RankedSource, ScrapedContent
from backend.retrieval import HybridConfig, HybridRetriever
from backend.retrieval.models import RetrievalDocument

_SNIPPET_CHARS = 200


def doc_source_id(doc: BenchDoc) -> str:
    return f"doc-{doc.doc_id}"


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _publish(doc: BenchDoc):
    from datetime import date

    return date.fromisoformat(doc.publish_date) if doc.publish_date else None


class FrozenRetrieval:
    def __init__(self, task: TaskDefinition) -> None:
        self._task = task
        self._retriever = HybridRetriever(embedder=None, config=HybridConfig())
        self._docs = [RetrievalDocument(doc_id=d.doc_id, text=d.content, title=d.title) for d in task.corpus]

    def retrieve_and_rank(self, query: str, top_k: int, research_mode: Any = None) -> tuple[list, list[RankedSource]]:
        result = self._retriever.retrieve(query, self._docs, top_k=top_k, use_dense=False, rerank=False)
        ranked: list[RankedSource] = []
        for position, hit in enumerate(result.hits, start=1):
            doc = self._task.doc(hit.doc_id)
            if doc is None:  # cannot happen (docs come from the task) — never trust it blindly
                continue
            ranked.append(RankedSource(
                source_id=doc_source_id(doc), title=doc.title, url=doc.url, domain=_domain(doc.url),
                snippet=doc.content[:_SNIPPET_CHARS], publish_date=_publish(doc),
                relevance_score=min(1.0, float(hit.score)), rank_position=position,
            ))
        return [], ranked


class FrozenScraping:
    def __init__(self, task: TaskDefinition) -> None:
        self._task = task

    async def scrape_sources(self, sources: list[RankedSource]) -> list[ScrapedContent]:
        out: list[ScrapedContent] = []
        for s in sources:
            doc = self._task.doc_by_url(str(s.url))
            if doc is None:
                out.append(ScrapedContent(source_id=s.source_id, url=str(s.url), title=s.title, domain=s.domain,
                                          content="", content_hash="", success=False,
                                          error_message="URL is not part of the frozen corpus."))
                continue
            out.append(ScrapedContent(
                source_id=s.source_id, url=doc.url, title=doc.title, domain=_domain(doc.url), publish_date=_publish(doc),
                content=doc.content, content_hash=hashlib.sha256(doc.content.encode("utf-8")).hexdigest(),
                success=True, content_length=len(doc.content)))
        return out


class FrozenWorld:
    def __init__(self, task: TaskDefinition, llm: Any, meter: UsageMeter | None = None) -> None:
        self.task = task
        self.llm = MeteredLLM(llm, meter)
        self.meter = self.llm.meter
        self._retrieval = FrozenRetrieval(task)
        self._scraping = FrozenScraping(task)

    def _verification_agent(self) -> Any:
        from backend.agents.verification_agent import VerificationAgent  # lazy: imports langchain

        return VerificationAgent(llm=self.llm)

    def registry(self):
        return build_default_registry(ToolDependencies(
            retrieval_service=lambda: self._retrieval,
            scraping_service=lambda: self._scraping,
            verification_agent=self._verification_agent,
            vector_memory=lambda: None,   # search_existing_evidence is not part of any bench strategy
        ))
