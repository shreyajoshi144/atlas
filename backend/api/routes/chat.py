from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException

from backend.models.api_models import ResearchChatRequest
from backend.models.response_models import (
    ApiResponse,
    ChatMessageResponse,
    ChatResponse,
    RetrievedChunkResponse,
)
from langchain_core.messages import HumanMessage, SystemMessage

router = APIRouter()


@lru_cache(maxsize=1)
def _vector_memory():
    from backend.memory import VectorMemoryService
    return VectorMemoryService()


@lru_cache(maxsize=1)
def _llm():
    from backend.core.llm import get_chat_llm
    return get_chat_llm()


@router.post("/research", response_model=ApiResponse[ChatResponse])
async def research_chat(request: ResearchChatRequest) -> ApiResponse[ChatResponse]:
    try:
        vector_memory = _vector_memory()

        # Convert history dicts/objects to simple role/content for context building
        history_items: list[dict[str, str]] = []
        for msg in request.history:
            if hasattr(msg, "role"):
                history_items.append({"role": msg.role, "content": msg.content})
            elif isinstance(msg, dict):
                history_items.append({"role": msg.get("role", ""), "content": msg.get("content", "")})

        # Retrieve from vector memory — guard against empty collection
        try:
            collection_count = vector_memory.collection.count()
        except Exception:
            collection_count = 0

        retrieved_chunks: list[dict[str, Any]] = []
        report_ids: list[str] = []

        if collection_count > 0:
            try:
                context = _retrieve_context(
                    vector_memory, request.topic, request.question,
                    history_items, request.top_k_chunks,
                )
                retrieved_chunks = context.get("retrieved_chunks", [])
                report_ids = context.get("report_ids", [])
            except Exception as exc:
                # Non-fatal: answer without context
                pass

        context_blocks = "\n\n".join(
            f"[Chunk] Topic: {item['topic']}\n"
            f"Similarity: {item['similarity_score']}\n"
            f"Content: {item['content'][:3000]}"
            for item in retrieved_chunks
        ) or "No relevant research context found."

        system_prompt = (
            "You are a research assistant for Atlas AI. "
            "Answer using the retrieved research context and conversation history. "
            "Be clear, accurate, and practical. "
            "If the context is insufficient, say so."
        )

        history_text = "\n".join(
            f"{msg['role']}: {msg['content']}"
            for msg in history_items[-6:]
        )

        human_prompt = f"""
Topic:
{request.topic}

Conversation History:
{history_text}

Retrieved Research Context:
{context_blocks}

User Question:
{request.question}

Answer clearly and contextually.
"""

        answer = _llm().invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ]
        ).content

        messages: list[ChatMessageResponse] = [
            ChatMessageResponse(role=msg["role"], content=msg["content"])
            for msg in history_items
        ] + [
            ChatMessageResponse(role="user", content=request.question),
            ChatMessageResponse(role="assistant", content=answer),
        ]

        chunk_resps = [
            RetrievedChunkResponse(
                report_id=chunk.get("report_id", ""),
                chunk_id=chunk.get("chunk_id", ""),
                session_id=chunk.get("session_id", ""),
                topic=chunk.get("topic", ""),
                chunk_index=chunk.get("chunk_index", 0),
                content=chunk.get("content", ""),
                similarity_score=chunk.get("similarity_score", 0.0),
            )
            for chunk in retrieved_chunks
        ]

        return ApiResponse(
            success=True,
            message="Research chat completed",
            data=ChatResponse(
                answer=answer,
                messages=messages,
                retrieved_chunks=chunk_resps,
                report_ids=report_ids,
            ),
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Research chat failed: {exc}"
        ) from exc


def _retrieve_context(
    vector_memory: Any,
    topic: str,
    question: str,
    history: list[dict[str, str]],
    top_k: int,
) -> dict[str, Any]:
    """Safe wrapper around retrieve_chat_context using plain dicts."""
    from backend.models.domain_models import ChatMessage

    history_msgs = [
        ChatMessage(role=m["role"], content=m["content"])
        for m in history
        if m.get("role") and m.get("content")
    ]

    # Try with topic filter first; fall back to no filter if collection too small
    try:
        return vector_memory.retrieve_chat_context(
            topic=topic,
            question=question,
            history=history_msgs,
            top_k=top_k,
        )
    except Exception:
        # Fall back: search without topic filter
        query_embedding = vector_memory.embedding_service.embed_text(
            f"Topic: {topic}\nQuestion: {question}"
        )
        collection_count = vector_memory.collection.count()
        n_results = min(top_k * 3, max(collection_count, 1))
        response = vector_memory.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        documents = response.get("documents", [[]])[0]
        metadatas = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]

        chunks = []
        report_ids = []
        for doc, meta, dist in zip(documents, metadatas, distances):
            report_id = meta.get("report_id", "")
            if report_id:
                report_ids.append(report_id)
                chunks.append({
                    "report_id": report_id,
                    "chunk_id": meta.get("chunk_id", ""),
                    "topic": meta.get("topic", ""),
                    "session_id": meta.get("session_id", ""),
                    "chunk_index": meta.get("chunk_index", 0),
                    "content": doc,
                    "similarity_score": round(max(0.0, 1.0 - float(dist)), 4),
                })

        chunks.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {"retrieved_chunks": chunks[:top_k], "report_ids": list(dict.fromkeys(report_ids))}
