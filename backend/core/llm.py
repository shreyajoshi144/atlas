from functools import lru_cache

from backend.core.config import get_settings
from backend.core.retry import invoke_with_retry
from langchain_groq import ChatGroq
from langchain_openai import OpenAIEmbeddings


@lru_cache(maxsize=1)
def get_chat_llm():
    """The chat model every agent uses (M9): a routing proxy with the same ``invoke`` surface.

    Agents and the graph are unchanged; which model actually answers is decided per call by the
    router from the caller's routing policy (see ``backend.routing``). With no policy and no user
    scope it behaves exactly like the plain application model.
    """
    from backend.routing.router import get_routed_llm  # lazy: routing imports this module

    return get_routed_llm()


@lru_cache(maxsize=1)
def get_default_chat_llm() -> ChatGroq:
    """The plain application model client (what ``get_chat_llm`` returned before M9)."""
    settings = get_settings()
    return ChatGroq(
        api_key=settings.groq_api_key,
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        timeout=settings.request_timeout_seconds,
    )


def build_chat_llm(model: str, *, temperature: float = 0.0, max_tokens: int = 4096, seed: int | None = None) -> ChatGroq:
    """Build an *uncached* chat model for one explicit configuration (AtlasBench).

    ``get_chat_llm`` is a process-wide singleton for the research pipeline; the
    benchmark must be able to run the same task under different models, so it
    builds one client per configuration. ``model`` is validated by the caller
    against an allowlist — this function never chooses a model itself.
    """
    settings = get_settings()
    kwargs = {"model_kwargs": {"seed": int(seed)}} if seed is not None else {}
    return ChatGroq(
        api_key=settings.groq_api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=settings.request_timeout_seconds,
        **kwargs,
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
        timeout=settings.request_timeout_seconds,
    )
