from __future__ import annotations
from typing import Any
from backend.memory.chroma_client import ChromaMemoryClient
from backend.memory.embedding_service import EmbeddingService, get_shared_embedding_service
from backend.retrieval import HybridRetriever, RetrievalDocument, RetrievalResult
from backend.core.logging import get_logger
from backend.database import ResearchRepository
from backend.models.domain_models import ChatMessage, ResearchReport, ResearchSession
from backend.models.response_models import SemanticSearchResultResponse

logger = get_logger(__name__)


class VectorMemoryService:
    def __init__(
        self,
        chroma_client: ChromaMemoryClient | None = None,
        embedding_service: EmbeddingService | None = None,
        repository: ResearchRepository | None = None,
        hybrid_retriever: HybridRetriever | None = None,
    ) -> None:
        self.chroma = chroma_client or ChromaMemoryClient()
        # Shared (cached, self-healing) service: building one per request would
        # reload the model every time.
        self.embedding_service = embedding_service or get_shared_embedding_service()
        self.hybrid = hybrid_retriever or HybridRetriever(self.embedding_service)
        self.repository = repository or ResearchRepository()
        self.collection = self.chroma.get_collection()

    def index_report(
        self,
        topic_id: str,
        session: ResearchSession,
        report: ResearchReport,
        user_id: str,
    ) -> None:
        """user_id is mandatory and must come from the authenticated caller
        (never the client) — it's stamped into every chunk's Chroma metadata
        so every later vector query can filter to just this user's data."""
        chunks = self._chunk_report(
            report_id=report.report_id,
            session_id=session.session_id,
            topic=report.topic,
            text=self._build_report_text(report),
        )

        self.repository.chunks.replace_chunks(
            report_id=report.report_id,
            session_id=session.session_id,
            topic_id=topic_id,
            topic=report.topic,
            chunks=chunks,
        )

        documents = [chunk["content"] for chunk in chunks]
        ids = [chunk["chunk_id"] for chunk in chunks]
        metadatas = [
            {
                "report_id": chunk["report_id"],
                "chunk_id": chunk["chunk_id"],
                "topic": chunk["topic"],
                "session_id": chunk["session_id"],
                "user_id": user_id,
                "document_type": "report_chunk",
                "chunk_index": chunk["chunk_index"],
                # Stamp the model so vectors from different models are detectable.
                "embedding_model": getattr(self.embedding_service, "model_id", "unknown"),
            }
            for chunk in chunks
        ]

        embeddings = self.embedding_service.embed_texts(documents)
        if len(embeddings) != len(documents):
            raise ValueError("Embedding generation failed for one or more chunks.")

        self.collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

        logger.info(
            "Indexed chunked report into vector memory",
            extra={
                "report_id": report.report_id,
                "session_id": session.session_id,
                "chunk_count": len(chunks),
            },
        )

    @staticmethod
    def _scoped_where(user_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Every vector query must be filtered by the authenticated user's
        user_id — this is mandatory, not optional, regardless of what other
        filters (topic, etc.) are also applied. Combine with $and rather
        than a flat multi-key dict, since Chroma requires the explicit
        combinator once more than one condition is present."""
        if not user_id:
            raise ValueError("user_id is required for every vector memory query.")
        conditions: list[dict[str, Any]] = [{"user_id": user_id}]
        if extra:
            conditions.extend({k: v} for k, v in extra.items())
        return conditions[0] if len(conditions) == 1 else {"$and": conditions}

    # ── hybrid retrieval ─────────────────────────────────────────────────────
    _VECTOR_BATCH = 500

    def _stored_vectors(self, chunk_ids: list[str], user_id: str) -> dict[str, list[float]]:
        """Read persisted vectors back from Chroma (scoped to the user).

        Failure here is not fatal: those chunks simply take part lexically.
        """
        vectors: dict[str, list[float]] = {}
        try:
            for i in range(0, len(chunk_ids), self._VECTOR_BATCH):
                batch = chunk_ids[i : i + self._VECTOR_BATCH]
                got = self.collection.get(ids=batch, where=self._scoped_where(user_id), include=["embeddings"])
                ids = got.get("ids") if got else None
                embs = got.get("embeddings") if got else None
                if ids is None or embs is None:
                    continue
                for cid, emb in zip(ids, embs):
                    if emb is not None and len(emb):
                        vectors[cid] = [float(x) for x in emb]
        except Exception as exc:
            logger.warning("Reading stored vectors failed — lexical-only for this query", extra={"error": str(exc)})
        return vectors

    def hybrid_search(
        self,
        query: str,
        user_id: str,
        *,
        limit: int = 5,
        topic: str | None = None,
        report_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        dense_query: str | None = None,
        diversify: bool = False,
    ) -> RetrievalResult:
        """BM25 + dense + metadata filters + rerank over ONE user's chunks.

        Ownership is enforced twice: the candidate set comes from an SQL join
        on the session's user_id, and vector reads are scoped by user_id in
        Chroma. ``filters`` is a generic metadata filter over report_id, topic,
        session_id, chunk_index, created_at and verification_score.
        """
        if not user_id:
            raise ValueError("user_id is required for every vector memory query.")
        max_scan = 5000
        try:
            from backend.core.config import get_settings

            max_scan = get_settings().retrieval_max_scan_chunks
        except Exception:
            pass
        rows = self.repository.chunks.get_user_chunks(user_id, topic=topic, report_ids=report_ids, limit=max_scan)
        if len(rows) >= max_scan:
            logger.warning("Memory scan hit the candidate cap; older chunks excluded", extra={"cap": max_scan})
        vectors = self._stored_vectors([r["chunk_id"] for r in rows], user_id) if rows else {}
        documents = [
            RetrievalDocument(
                doc_id=r["chunk_id"],
                text=r["content"],
                title=r["topic"] or "",
                metadata={
                    "report_id": r["report_id"], "topic": r["topic"], "session_id": r["session_id"],
                    "chunk_index": r["chunk_index"], "created_at": r["created_at"],
                    "verification_score": r["verification_score"],
                },
                embedding=vectors.get(r["chunk_id"]),
            )
            for r in rows
        ]
        return self.hybrid.retrieve(
            query, documents, top_k=limit, filters=filters, dense_query=dense_query,
            diversify=diversify, embed_missing=False,
        )

    def semantic_search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
    ) -> list[SemanticSearchResultResponse]:
        result = self.hybrid_search(query, user_id, limit=max(limit * 3, limit))
        best_by_report: dict[str, SemanticSearchResultResponse] = {}
        for hit in result.hits:
            report_id = hit.document.metadata["report_id"]
            # Scoped again at the SQL layer — defense in depth.
            report_row = self.repository.reports.get_report(report_id=report_id, user_id=user_id)
            if not report_row:
                continue
            candidate = SemanticSearchResultResponse(
                report_id=report_id,
                topic=report_row.get("topic", ""),
                summary=report_row.get("summary", ""),
                similarity_score=round(hit.score, 4),
            )
            existing = best_by_report.get(report_id)
            if existing is None or candidate.similarity_score > existing.similarity_score:
                best_by_report[report_id] = candidate

        results = sorted(best_by_report.values(), key=lambda item: item.similarity_score, reverse=True)[:limit]
        logger.info(
            "Semantic search completed",
            extra={"query": query, "result_count": len(results), "retrieval_mode": result.diagnostics.mode},
        )
        return results

    def retrieve_chat_context(
        self,
        topic: str,
        question: str,
        history: list[ChatMessage],
        user_id: str,
        top_k: int = 4,
    ) -> dict[str, Any]:
        """user_id is mandatory — Ask Atlas must never surface another
        user's report chunks, however close the topic match is."""
        history_text = "\n".join(f"{message.role}: {message.content}" for message in history[-6:])
        dense_query = "\n".join(
            [f"Topic: {topic}", f"Question: {question}", f"Conversation History: {history_text}"]
        ).strip()

        # BM25 sees the focused question; the dense side sees the richer context.
        result = self.hybrid_search(
            question, user_id, limit=max(top_k * 3, top_k), topic=topic,
            dense_query=dense_query, diversify=True,
        )
        retrieved_chunks: list[dict[str, Any]] = []
        report_ids: list[str] = []
        owned: dict[str, bool] = {}
        for hit in result.hits:
            meta = hit.document.metadata
            report_id = meta["report_id"]
            if report_id not in owned:
                owned[report_id] = bool(self.repository.reports.get_report(report_id=report_id, user_id=user_id))
            if not owned[report_id]:
                continue
            report_ids.append(report_id)
            retrieved_chunks.append(
                {
                    "report_id": report_id,
                    "chunk_id": hit.doc_id,
                    "topic": meta["topic"],
                    "session_id": meta["session_id"],
                    "chunk_index": meta["chunk_index"],
                    "content": hit.document.text,
                    "similarity_score": round(hit.score, 4),
                }
            )
        retrieved_chunks = retrieved_chunks[:top_k]
        report_ids = [c["report_id"] for c in retrieved_chunks]

        logger.info(
            "Retrieved chat context from vector memory",
            extra={"topic": topic, "question_length": len(question), "chunk_count": len(retrieved_chunks),
                   "retrieval_mode": result.diagnostics.mode},
        )
        return {"retrieved_chunks": retrieved_chunks, "report_ids": list(dict.fromkeys(report_ids))}

    def delete_report_memory(self, report_id: str, user_id: str) -> None:
        """Callers must verify report ownership before calling this (routes
        already do, via ReportRepository.is_owned_by). user_id is required
        here too and passed as an extra Chroma-side filter so a delete can
        never remove another user's vectors even if report_id were reused
        or a caller's ownership check were ever bypassed."""
        chunks = self.repository.chunks.get_chunks_by_report_id(report_id=report_id)
        ids = [chunk["chunk_id"] for chunk in chunks]
        if ids:
            self.collection.delete(
                ids=ids,
                where=self._scoped_where(user_id, extra={"report_id": report_id}),
            )
        # Chunk rows are the hybrid candidate source, so they go too — scoped
        # to the owner, so a mismatched user_id still deletes nothing.
        self.repository.chunks.delete_chunks_for_report(report_id, user_id)

        logger.info(
            "Deleted report chunks from vector memory",
            extra={"report_id": report_id, "chunk_count": len(ids)},
        )

    def _chunk_report(
        self,
        report_id: str,
        session_id: str,
        topic: str,
        text: str,
        chunk_size: int = 800,
        overlap: int = 120,
    ) -> list[dict[str, Any]]:
        cleaned = " ".join(text.split())
        if not cleaned:
            return []

        if chunk_size < 500 or chunk_size > 1000:
            raise ValueError("chunk_size must be between 500 and 1000 characters.")

        chunks: list[dict[str, Any]] = []
        start = 0
        chunk_index = 0

        while start < len(cleaned):
            end = min(start + chunk_size, len(cleaned))
            window = cleaned[start:end]

            if end < len(cleaned):
                last_break = max(
                    window.rfind(". "),
                    window.rfind("\n"),
                    window.rfind(" "),
                )
                if last_break > 500:
                    end = start + last_break + 1
                    window = cleaned[start:end]

            content = window.strip()
            if content:
                chunk_id = f"{report_id}:chunk:{chunk_index:04d}"
                chunks.append(
                    {
                        "report_id": report_id,
                        "chunk_id": chunk_id,
                        "topic": topic,
                        "session_id": session_id,
                        "chunk_index": chunk_index,
                        "content": content,
                    }
                )
                chunk_index += 1

            if end >= len(cleaned):
                break

            start = max(end - overlap, start + 1)

        return chunks

    def _build_report_text(self, report: ResearchReport) -> str:
        parts: list[str] = [
            f"Topic: {report.topic}",
            f"Summary: {report.summary}",
            f"Report: {report.report_markdown}",
        ]

        if report.key_statistics:
            parts.append("Key Statistics: " + " | ".join(report.key_statistics))

        if report.verification:
            parts.append(
                f"Verification Score: {report.verification.overall_verification_score}"
            )
            parts.append(f"Verification Verdict: {report.verification.verdict}")

        for source in report.sources:
            parts.append(
                f"Source: {source.title} | {source.domain} | relevance={source.relevance_score} | credibility={source.credibility_score}"
            )

        return "\n\n".join(parts).strip()
    def search_similar(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Alias used by the search route. Returns raw dicts instead of
        response models. user_id is mandatory — semantic search must never
        return another user's reports, topic filter or not."""
        try:
            result = self.hybrid_search(query, user_id, limit=max(limit * 3, limit), topic=topic)
        except Exception as exc:
            logger.warning("search_similar query failed", extra={"error": str(exc), "error_type": type(exc).__name__})
            return []

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in result.hits:
            report_id = hit.document.metadata["report_id"]
            if report_id in seen:
                continue
            seen.add(report_id)
            results.append({
                "report_id": report_id,
                "topic": hit.document.metadata.get("topic", ""),
                "similarity_score": round(hit.score, 4),
            })
        results.sort(key=lambda x: x["similarity_score"], reverse=True)
        return results[:limit]
