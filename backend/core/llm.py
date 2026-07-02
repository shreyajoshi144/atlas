from functools import lru_cache
from backend.core.config import get_settings
from langchain_groq import ChatGroq
from langchain_openai import OpenAIEmbeddings

@lru_cache(maxsize=1)
def get_chat_llm() -> ChatGroq:
    settings = get_settings()
    return ChatGroq(
        api_key=settings.groq_api_key,
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
    )

@lru_cache(maxsize=1)
def get_embedding_model() -> OpenAIEmbeddings:
    """
    Embeddings still use OpenAI text-embedding-3-small.
    If you don't have OpenAI credits, ChromaDB will fall back gracefully
    and semantic search / chat won't work — but research itself will.
    """
    settings = get_settings()
    return OpenAIEmbeddings(
        api_key=settings.openai_api_key,
        model="text-embedding-3-small",
    )
