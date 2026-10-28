"""
Cross-user authorization / data-isolation tests for Atlas AI.

These tests do NOT require GROQ_API_KEY / TAVILY_API_KEY to be real, and do
NOT call any external network service (Groq, Tavily, HuggingFace). The LLM
call in chat.py and the research pipeline itself are out of scope here —
this suite targets exactly the thing the audit was about: given two
authenticated users, can User B ever read, search, chat-retrieve, or delete
User A's data. Embeddings are faked (deterministic, hash-based) so the
suite runs fully offline and fast; that's fine because what's under test is
the *filtering logic* around the vector store, not embedding quality.

Run with:
    GROQ_API_KEY=test TAVILY_API_KEY=test python -m pytest backend/tests/test_authorization_isolation.py -v
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")


# ── Deterministic fake embeddings (no sentence-transformers / network) ──────
def _fake_vector(text: str, dim: int = 16) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in digest[:dim]]


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    from backend.memory.embedding_service import EmbeddingService

    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, text: _fake_vector(text))
    monkeypatch.setattr(
        EmbeddingService, "embed_texts", lambda self, texts: [_fake_vector(t) for t in texts]
    )


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """Fresh SQLite + Chroma paths per test, and a clean get_settings() cache
    so each test is fully isolated from any other test or real dev data."""
    from backend.core import config as config_module

    sqlite_path = tmp_path / "sqlite" / "atlas.db"
    chroma_path = tmp_path / "chroma"
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("CHROMA_PATH", str(chroma_path))
    config_module.get_settings.cache_clear()
    yield sqlite_path, chroma_path
    config_module.get_settings.cache_clear()


@pytest.fixture()
def repository(isolated_paths):
    from backend.database import ResearchRepository

    repo = ResearchRepository()
    repo.initialize()
    return repo


@pytest.fixture()
def vector_memory(repository):
    from backend.memory import VectorMemoryService

    return VectorMemoryService(repository=repository)


def _make_user(repository, name: str) -> dict:
    from backend.auth.security import hash_password

    password_hash, salt = hash_password("Sup3rSecret!")
    return repository.users.create(
        email=f"{name.lower()}@example.com",
        name=name,
        password_hash=password_hash,
        password_salt=salt,
    )


def _make_report_for_user(repository, vector_memory, user_id: str, topic: str, report_text: str):
    """Create a full session + report + chunks + Chroma vectors for a user,
    bypassing the LangGraph research pipeline (which needs real Groq/Tavily
    keys) — this exercises the same repository/VectorMemoryService code the
    real pipeline calls, just without the LLM/search steps in between."""
    from backend.models.domain_models import ResearchReport, ResearchSession

    topic_id = repository.topics.get_or_create_topic(topic, user_id=user_id)

    session = ResearchSession(topic=topic, status="completed")
    repository.sessions.save_session(topic_id, session, user_id=user_id)

    report = ResearchReport(
        report_id=f"report_{uuid.uuid4().hex[:12]}",
        topic=topic,
        summary=f"Summary for {report_text}",
        report_markdown=report_text,
        key_statistics=[],
        swot={},
    )
    repository.reports.save_report(topic_id, session, report)

    vector_memory.index_report(topic_id=topic_id, session=session, report=report, user_id=user_id)

    return session, report


# ── 1. Repository-level isolation ───────────────────────────────────────────
def test_report_repository_isolation(repository, vector_memory):
    user_a = _make_user(repository, "Alice")
    user_b = _make_user(repository, "Bob")

    _, report_a = _make_report_for_user(repository, vector_memory, user_a["user_id"], "Fusion energy", "A's report")
    _, report_b = _make_report_for_user(repository, vector_memory, user_b["user_id"], "Fusion energy", "B's report")

    # Each user only sees their own report in listings
    a_reports = {r["report_id"] for r in repository.reports.list_reports(user_id=user_a["user_id"])}
    b_reports = {r["report_id"] for r in repository.reports.list_reports(user_id=user_b["user_id"])}
    assert a_reports == {report_a.report_id}
    assert b_reports == {report_b.report_id}

    # User B cannot fetch or claim ownership of User A's report
    assert repository.reports.get_report(report_a.report_id, user_id=user_b["user_id"]) is None
    assert repository.reports.is_owned_by(report_a.report_id, user_b["user_id"]) is False

    # User A can fetch their own report
    assert repository.reports.get_report(report_a.report_id, user_id=user_a["user_id"]) is not None
    assert repository.reports.is_owned_by(report_a.report_id, user_a["user_id"]) is True


def test_topics_are_per_user(repository):
    user_a = _make_user(repository, "Alice2")
    user_b = _make_user(repository, "Bob2")

    tid_a = repository.topics.get_or_create_topic("Shared Topic Name", user_id=user_a["user_id"])
    tid_b = repository.topics.get_or_create_topic("Shared Topic Name", user_id=user_b["user_id"])
    tid_a_again = repository.topics.get_or_create_topic("Shared Topic Name", user_id=user_a["user_id"])

    assert tid_a != tid_b, "two users with the same topic text must not share a topic row"
    assert tid_a == tid_a_again, "the same user reusing a topic text must reuse the row"


def test_analytics_and_knowledge_base_are_scoped(repository, vector_memory):
    user_a = _make_user(repository, "Alice3")
    user_b = _make_user(repository, "Bob3")
    _make_report_for_user(repository, vector_memory, user_a["user_id"], "Topic A3", "content a3")
    _make_report_for_user(repository, vector_memory, user_b["user_id"], "Topic B3", "content b3")
    _make_report_for_user(repository, vector_memory, user_b["user_id"], "Topic B3b", "content b3b")

    totals_a = repository.analytics.get_totals(user_id=user_a["user_id"])
    totals_b = repository.analytics.get_totals(user_id=user_b["user_id"])
    assert totals_a["total_reports"] == 1
    assert totals_b["total_reports"] == 2

    kb_a = repository.knowledge_base.get_overview(user_id=user_a["user_id"])
    kb_b = repository.knowledge_base.get_overview(user_id=user_b["user_id"])
    assert kb_a["total_reports"] == 1
    assert kb_b["total_reports"] == 2
    assert set(kb_a["report_ids"]).isdisjoint(set(kb_b["report_ids"]))


# ── 2. Vector memory (Chroma) isolation ─────────────────────────────────────
def test_vector_search_similar_is_user_scoped(repository, vector_memory):
    user_a = _make_user(repository, "Alice4")
    user_b = _make_user(repository, "Bob4")

    _, report_a = _make_report_for_user(
        repository, vector_memory, user_a["user_id"], "Battery chemistry",
        "Solid-state batteries improve energy density substantially for electric vehicles.",
    )
    _, report_b = _make_report_for_user(
        repository, vector_memory, user_b["user_id"], "Battery chemistry",
        "Solid-state batteries improve energy density substantially for electric vehicles.",
    )

    results_a = vector_memory.search_similar(query="solid-state battery energy density", user_id=user_a["user_id"])
    results_b = vector_memory.search_similar(query="solid-state battery energy density", user_id=user_b["user_id"])

    ids_a = {r["report_id"] for r in results_a}
    ids_b = {r["report_id"] for r in results_b}

    assert report_a.report_id in ids_a
    assert report_b.report_id not in ids_a, "User A's search must never surface User B's report"
    assert report_b.report_id in ids_b
    assert report_a.report_id not in ids_b, "User B's search must never surface User A's report"


def test_vector_chat_retrieval_is_user_scoped(repository, vector_memory):
    from backend.models.domain_models import ChatMessage

    user_a = _make_user(repository, "Alice5")
    user_b = _make_user(repository, "Bob5")

    _, report_a = _make_report_for_user(
        repository, vector_memory, user_a["user_id"], "Space exploration",
        "Mars missions require radiation shielding and closed-loop life support.",
    )
    _, report_b = _make_report_for_user(
        repository, vector_memory, user_b["user_id"], "Space exploration",
        "Mars missions require radiation shielding and closed-loop life support.",
    )

    ctx_a = vector_memory.retrieve_chat_context(
        topic="Space exploration", question="What do Mars missions need?",
        history=[], user_id=user_a["user_id"],
    )
    ctx_b = vector_memory.retrieve_chat_context(
        topic="Space exploration", question="What do Mars missions need?",
        history=[], user_id=user_b["user_id"],
    )

    assert all(c["report_id"] == report_a.report_id for c in ctx_a["retrieved_chunks"])
    assert all(c["report_id"] == report_b.report_id for c in ctx_b["retrieved_chunks"])
    assert report_a.report_id in ctx_a["report_ids"]
    assert report_b.report_id not in ctx_a["report_ids"]


def test_chat_route_fallback_path_is_user_scoped(repository, vector_memory):
    """The chat route's fallback (used when the topic-filtered query raises)
    used to run a completely unfiltered Chroma query. Exercise that exact
    fallback function directly to make sure it's user-scoped now."""
    from backend.api.routes.chat import _retrieve_context

    user_a = _make_user(repository, "Alice6")
    user_b = _make_user(repository, "Bob6")

    _, report_a = _make_report_for_user(
        repository, vector_memory, user_a["user_id"], "Climate policy",
        "Carbon pricing mechanisms vary widely between national jurisdictions.",
    )
    _, report_b = _make_report_for_user(
        repository, vector_memory, user_b["user_id"], "Climate policy",
        "Carbon pricing mechanisms vary widely between national jurisdictions.",
    )

    # Force the fallback branch by making retrieve_chat_context raise.
    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure to hit the fallback path")

    vector_memory.retrieve_chat_context = _boom  # type: ignore[method-assign]

    ctx_a = _retrieve_context(
        vector_memory, "Climate policy", "How does carbon pricing vary?", [], top_k=4,
        user_id=user_a["user_id"],
    )
    report_ids_a = set(ctx_a["report_ids"])
    assert report_a.report_id in report_ids_a
    assert report_b.report_id not in report_ids_a, "fallback path leaked another user's report"


def test_delete_report_memory_only_deletes_own_vectors(repository, vector_memory):
    user_a = _make_user(repository, "Alice7")
    user_b = _make_user(repository, "Bob7")

    _, report_a = _make_report_for_user(
        repository, vector_memory, user_a["user_id"], "Deletion test",
        "Some content for a report that will be deleted.",
    )

    # Wrong user attempting the delete must remove nothing.
    vector_memory.delete_report_memory(report_id=report_a.report_id, user_id=user_b["user_id"])
    still_there = vector_memory.search_similar(query="content for a report", user_id=user_a["user_id"])
    assert any(r["report_id"] == report_a.report_id for r in still_there), (
        "a mismatched user_id must not be able to delete another user's vectors"
    )

    # Correct owner can delete it.
    vector_memory.delete_report_memory(report_id=report_a.report_id, user_id=user_a["user_id"])
    gone = vector_memory.search_similar(query="content for a report", user_id=user_a["user_id"])
    assert not any(r["report_id"] == report_a.report_id for r in gone)


# ── 3. Route-level isolation (FastAPI TestClient + auth dependency override) ─
@pytest.fixture()
def api_client(isolated_paths):
    """A TestClient with get_current_user overridden per-request via a
    mutable holder, so tests can flip between "logged in as A" and "logged
    in as B" without re-registering real accounts or minting real tokens."""
    import importlib

    import backend.api.routes.chat as chat_route
    import backend.api.routes.knowledge_base as kb_route
    import backend.api.routes.notes as notes_route
    import backend.api.routes.search as search_route
    import main as main_module
    importlib.reload(main_module)  # fresh app per test, fresh lru_cache()s

    from backend.auth.dependencies import get_current_user
    from backend.database import ResearchRepository
    from fastapi.testclient import TestClient

    current = {"user": None}

    async def _override():
        return current["user"]

    main_module.app.dependency_overrides[get_current_user] = _override

    repo = ResearchRepository()
    repo.initialize()

    with TestClient(main_module.app) as client:
        yield client, current, repo

    main_module.app.dependency_overrides.clear()


def test_notes_route_cross_user_isolation(api_client, monkeypatch):
    client, current, repository = api_client

    user_a = _make_user(repository, "Alice8")
    user_b = _make_user(repository, "Bob8")

    from backend.memory import VectorMemoryService
    vm = VectorMemoryService(repository=repository)
    _, report_a = _make_report_for_user(repository, vm, user_a["user_id"], "Notes topic", "report a content")

    # User A can write and read their own note.
    current["user"] = user_a
    r = client.put(f"/api/notes/{report_a.report_id}", json={"content": "my private note"})
    assert r.status_code == 200
    r = client.get(f"/api/notes/{report_a.report_id}")
    assert r.status_code == 200
    assert r.json()["data"]["content"] == "my private note"

    # User B must not be able to read, overwrite, or delete User A's note.
    current["user"] = user_b
    r = client.get(f"/api/notes/{report_a.report_id}")
    assert r.status_code == 404
    r = client.put(f"/api/notes/{report_a.report_id}", json={"content": "hijacked"})
    assert r.status_code == 404
    r = client.delete(f"/api/notes/{report_a.report_id}")
    assert r.status_code == 404

    # User A's note is unaffected by B's attempts.
    current["user"] = user_a
    r = client.get(f"/api/notes/{report_a.report_id}")
    assert r.json()["data"]["content"] == "my private note"


def test_history_route_cross_user_isolation(api_client):
    client, current, repository = api_client

    user_a = _make_user(repository, "Alice9")
    user_b = _make_user(repository, "Bob9")

    from backend.memory import VectorMemoryService
    vm = VectorMemoryService(repository=repository)
    _, report_a = _make_report_for_user(repository, vm, user_a["user_id"], "History topic", "report a content")

    current["user"] = user_b
    r = client.get(f"/api/history/report/{report_a.report_id}")
    assert r.status_code == 404

    r = client.get("/api/history/reports")
    assert r.status_code == 200
    assert report_a.report_id not in {item["report_id"] for item in r.json()["data"]["reports"]}

    current["user"] = user_a
    r = client.get(f"/api/history/report/{report_a.report_id}")
    assert r.status_code == 200


def test_knowledge_base_delete_cross_user_isolation(api_client):
    client, current, repository = api_client

    user_a = _make_user(repository, "AliceA")
    user_b = _make_user(repository, "BobA")

    from backend.memory import VectorMemoryService
    vm = VectorMemoryService(repository=repository)
    _, report_a = _make_report_for_user(repository, vm, user_a["user_id"], "KB topic", "report a content")

    current["user"] = user_b
    r = client.delete(f"/api/knowledge-base/report/{report_a.report_id}")
    assert r.status_code == 404
    # Still there afterwards
    assert repository.reports.get_report(report_a.report_id, user_id=user_a["user_id"]) is not None

    current["user"] = user_a
    r = client.delete(f"/api/knowledge-base/report/{report_a.report_id}")
    assert r.status_code == 200
    assert repository.reports.get_report(report_a.report_id, user_id=user_a["user_id"]) is None


def test_semantic_search_route_cross_user_isolation(api_client):
    client, current, repository = api_client

    user_a = _make_user(repository, "AliceB")
    user_b = _make_user(repository, "BobB")

    from backend.memory import VectorMemoryService
    vm = VectorMemoryService(repository=repository)
    _, report_a = _make_report_for_user(
        repository, vm, user_a["user_id"], "Search topic",
        "Renewable energy grid storage using flow batteries.",
    )
    _, report_b = _make_report_for_user(
        repository, vm, user_b["user_id"], "Search topic",
        "Renewable energy grid storage using flow batteries.",
    )

    current["user"] = user_a
    r = client.post("/api/search/semantic", json={"query": "renewable energy grid storage"})
    assert r.status_code == 200
    ids = {item["report_id"] for item in r.json()["data"]["results"]}
    assert report_a.report_id in ids
    assert report_b.report_id not in ids

    current["user"] = user_b
    r = client.post("/api/search/semantic", json={"query": "renewable energy grid storage"})
    ids = {item["report_id"] for item in r.json()["data"]["results"]}
    assert report_b.report_id in ids
    assert report_a.report_id not in ids


def test_chat_route_cross_user_isolation(api_client, monkeypatch):
    """Full route test for /api/chat/research, with the LLM call stubbed out
    (no Groq key/network needed) so only the retrieval scoping is under
    test — the same thing that mattered for the search route above."""
    client, current, repository = api_client

    user_a = _make_user(repository, "AliceC")
    user_b = _make_user(repository, "BobC")

    from backend.memory import VectorMemoryService
    vm = VectorMemoryService(repository=repository)
    _, report_a = _make_report_for_user(
        repository, vm, user_a["user_id"], "Chat topic",
        "Quantum error correction uses redundant physical qubits per logical qubit.",
    )
    _, report_b = _make_report_for_user(
        repository, vm, user_b["user_id"], "Chat topic",
        "Quantum error correction uses redundant physical qubits per logical qubit.",
    )

    import backend.api.routes.chat as chat_route

    class _FakeLLMOutput:
        def __init__(self, chunks):
            self.answer = "stubbed answer"
            self.suggested_questions = []
            self._chunks = chunks

    def _fake_invoke_with_retry(runnable, messages, *a, **kw):
        # Capture whatever context ended up in the prompt so we can assert
        # on it via the human message content.
        human = messages[-1].content
        return _FakeLLMOutput(human)

    monkeypatch.setattr(chat_route, "_llm", lambda: object())
    monkeypatch.setattr("backend.core.llm.invoke_with_retry", _fake_invoke_with_retry)

    current["user"] = user_a
    r = client.post("/api/chat/research", json={"topic": "Chat topic", "question": "How does error correction work?"})
    assert r.status_code == 200
    data = r.json()["data"]
    report_ids = set(data["report_ids"])
    assert report_a.report_id in report_ids
    assert report_b.report_id not in report_ids, "chat retrieval leaked another user's report into context"

    current["user"] = user_b
    r = client.post("/api/chat/research", json={"topic": "Chat topic", "question": "How does error correction work?"})
    data = r.json()["data"]
    report_ids = set(data["report_ids"])
    assert report_b.report_id in report_ids
    assert report_a.report_id not in report_ids
