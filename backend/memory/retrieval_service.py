from __future__ import annotations
from typing import Any
from backend.memory.chroma_client import ChromaMemoryClient
from backend.memory.embedding_service import EmbeddingService
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
    ) -> None:
        self.chroma = chroma_client or ChromaMemoryClient()
        self.embedding_service = embedding_service or EmbeddingService()
        self.repository = repository or ResearchRepository()
        self.collection = self.chroma.get_collection()

    def index_report(
        self,
        topic_id: str,
        session: ResearchSession,
        report: ResearchReport,
    ) -> None:
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
                "document_type": "report_chunk",
                "chunk_index": chunk["chunk_index"],
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

    def semantic_search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[SemanticSearchResultResponse]:
        query_embedding = self.embedding_service.embed_text(query)
        response = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=max(limit * 3, limit),
            include=["metadatas", "distances", "documents"],
        )

        metadatas = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]
        best_by_report: dict[str, SemanticSearchResultResponse] = {}

        for metadata, distance in zip(metadatas, distances):
            report_id = metadata["report_id"]
            similarity_score = max(0.0, 1.0 - float(distance))

            report_row = self.repository.reports.get_report(report_id=report_id)
            if not report_row:
                continue

            candidate = SemanticSearchResultResponse(
                report_id=report_id,
                topic=report_row.get("topic", ""),
                summary=report_row.get("summary", ""),
                similarity_score=round(similarity_score, 4),
            )

            existing = best_by_report.get(report_id)
            if existing is None or candidate.similarity_score > existing.similarity_score:
                best_by_report[report_id] = candidate

        results = sorted(
            best_by_report.values(),
            key=lambda item: item.similarity_score,
            reverse=True,
        )[:limit]

        logger.info(
            "Semantic search completed",
            extra={"query": query, "result_count": len(results)},
        )
        return results

    def retrieve_chat_context(
        self,
        topic: str,
        question: str,
        history: list[ChatMessage],
        top_k: int = 4,
    ) -> dict[str, Any]:
        history_text = "\n".join(
            f"{message.role}: {message.content}"
            for message in history[-6:]
        )
        combined_query = "\n".join(
            [
                f"Topic: {topic}",
                f"Question: {question}",
                f"Conversation History: {history_text}",
            ]
        ).strip()

        query_embedding = self.embedding_service.embed_text(combined_query)
        response = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=max(top_k * 3, top_k),
            where={"topic": topic},
            include=["documents", "metadatas", "distances"],
        )

        documents = response.get("documents", [[]])[0]
        metadatas = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]

        retrieved_chunks: list[dict[str, Any]] = []
        report_ids: list[str] = []

        for document, metadata, distance in zip(documents, metadatas, distances):
            similarity = max(0.0, 1.0 - float(distance))
            report_id = metadata.get("report_id", "")
            if not report_id:
                continue

            report_row = self.repository.reports.get_report(report_id=report_id)
            if not report_row:
                continue

            report_ids.append(report_id)
            retrieved_chunks.append(
                {
                    "report_id": report_id,
                    "chunk_id": metadata.get("chunk_id", ""),
                    "topic": metadata.get("topic", ""),
                    "session_id": metadata.get("session_id", ""),
                    "chunk_index": metadata.get("chunk_index", 0),
                    "content": document,
                    "similarity_score": round(similarity, 4),
                }
            )

        retrieved_chunks = sorted(
            retrieved_chunks,
            key=lambda item: item["similarity_score"],
            reverse=True,
        )[:top_k]

        logger.info(
            "Retrieved chat context from vector memory",
            extra={
                "topic": topic,
                "question_length": len(question),
                "chunk_count": len(retrieved_chunks),
            },
        )

        return {
            "retrieved_chunks": retrieved_chunks,
            "report_ids": list(dict.fromkeys(report_ids)),
        }

    def delete_report_memory(self, report_id: str) -> None:
        chunks = self.repository.chunks.get_chunks_by_report_id(report_id=report_id)
        ids = [chunk["chunk_id"] for chunk in chunks]
        if ids:
            self.collection.delete(ids=ids)

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
        limit: int = 5,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Alias used by the search route. Returns raw dicts instead of response models."""
        try:
            query_embedding = self.embedding_service.embed_text(query)
            n_results = max(limit * 3, limit)
            collection_count = self.collection.count()
            if collection_count == 0:
                return []
            n_results = min(n_results, collection_count)

            query_kwargs: dict[str, Any] = dict(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["metadatas", "distances", "documents"],
            )
            if topic:
                query_kwargs["where"] = {"topic": topic}

            response = self.collection.query(**query_kwargs)
        except Exception as exc:
            logger.warning("search_similar query failed", extra={"error": str(exc)})
            return []

        metadatas = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for metadata, distance in zip(metadatas, distances):
            report_id = metadata.get("report_id", "")
            if not report_id or report_id in seen:
                continue
            seen.add(report_id)
            similarity_score = max(0.0, 1.0 - float(distance))
            results.append({
                "report_id": report_id,
                "topic": metadata.get("topic", ""),
                "similarity_score": round(similarity_score, 4),
            })

        results.sort(key=lambda x: x["similarity_score"], reverse=True)
        return results[:limit]
