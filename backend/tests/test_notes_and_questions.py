"""
Sprint 1A tests: My Notes (regression) + Questions I Still Need to Answer
(new) + Research-this-question provenance (new).

Fully offline — no Groq/Tavily calls. The research pipeline itself
(build_research_graph) is stubbed for the provenance test, since actually
running it needs real LLM/search access; what's under test there is the
source_report_id ownership check, not the pipeline.

Run with:
    GROQ_API_KEY=test TAVILY_API_KEY=test python -m pytest backend/tests/test_notes_and_questions.py -v
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("GROQ_API_KEY", "test_key")
os.environ.setdefault("TAVILY_API_KEY", "test_key")


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    from backend.core import config as config_module

    sqlite_path = tmp_path / "sqlite" / "atlas.db"
    chroma_path = tmp_path / "chroma"
    monkeypatch.setenv("SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("CHROMA_PATH", str(chroma_path))
    config_module.get_settings.cache_clear()

    # research.py memoizes its repository/vector-memory singletons with
    # @lru_cache(maxsize=1) — correct for a real long-running process (one
    # fixed SQLITE_PATH for the process lifetime), but it means the FIRST
    # test in a session to actually exercise persistence pins those
    # singletons to that test's tmp_path for every test after it. Clear
    # them here so each test gets its own, matching its own isolated_paths.
    import backend.api.routes.research as research_route

    research_route._repository.cache_clear()
    research_route._vector_memory.cache_clear()

    yield sqlite_path, chroma_path
    config_module.get_settings.cache_clear()


@pytest.fixture()
def repository(isolated_paths):
    from backend.database import ResearchRepository

    repo = ResearchRepository()
    repo.initialize()
    return repo


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from backend.auth.rate_limit import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture()
def api_client(isolated_paths):
    import importlib

    import main as main_module
    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as client:
        yield client


def _make_user(repository, name: str) -> dict:
    from backend.auth.security import hash_password

    password_hash, salt = hash_password("Sup3rSecret!")
    return repository.users.create(
        email=f"{name.lower()}@example.com",
        name=name,
        password_hash=password_hash,
        password_salt=salt,
    )


def _make_report(
    repository,
    user_id: str,
    *,
    topic: str = "Test topic",
    verification: dict | None = None,
    sources: list[dict] | None = None,
):
    """Builds a session + report (+ optional sources) directly through the
    repository layer, bypassing the LangGraph pipeline (needs real
    Groq/Tavily). Mirrors what research.py itself does after a real run."""
    import uuid

    from backend.models.domain_models import RankedSource, ResearchReport, ResearchSession

    topic_id = repository.topics.get_or_create_topic(topic, user_id=user_id)
    session = ResearchSession(topic=topic, status="completed")
    repository.sessions.save_session(topic_id, session, user_id=user_id)

    report = ResearchReport(
        report_id=f"report_{uuid.uuid4().hex[:12]}",
        topic=topic,
        summary="A summary",
        report_markdown="# Report",
        key_statistics=[],
        swot={},
    )
    repository.reports.save_report(topic_id, session, report)

    if verification is not None:
        # save_report only persists verification if it's on the domain
        # object, so patch the row directly to control exact JSON shape
        # (this test cares about verification_json contents specifically).
        import json as _json

        with repository.db.get_connection() as conn:
            conn.execute(
                "UPDATE reports SET verification_json = ?, verification_score = ?, verification_verdict = ? WHERE report_id = ?",
                (
                    _json.dumps(verification),
                    verification.get("overall_verification_score"),
                    verification.get("verdict"),
                    report.report_id,
                ),
            )
            conn.commit()

    if sources:
        ranked = [
            RankedSource(
                title=s.get("title", "Source"),
                url=s["url"],
                domain=s["domain"],
                snippet=s.get("snippet", ""),
                publish_date=s.get("publish_date"),
                credibility_score=s.get("credibility_score", 0.5),
                relevance_score=s.get("relevance_score", 0.5),
                rank_position=i,
            )
            for i, s in enumerate(sources)
        ]
        repository.sources.replace_sources(report.report_id, session.session_id, ranked)

    return session, report


# ── 1. My Notes — regression (create/read/update/delete + cross-user) ──────
def test_notes_full_crud_and_cross_user_isolation(api_client, repository):
    user_a = _make_user(repository, "NotesAlice")
    user_b = _make_user(repository, "NotesBob")
    _, report_a = _make_report(repository, user_a["user_id"], topic="Notes topic A")

    import main as main_module
    from backend.auth.dependencies import get_current_user

    current = {"user": user_a}

    async def _override():
        return current["user"]

    main_module.app.dependency_overrides[get_current_user] = _override
    client = api_client

    try:
        # Create (PUT with no prior note = create)
        r = client.put(f"/api/notes/{report_a.report_id}", json={"content": "first draft"})
        assert r.status_code == 200
        assert r.json()["data"]["content"] == "first draft"

        # Read
        r = client.get(f"/api/notes/{report_a.report_id}")
        assert r.status_code == 200
        assert r.json()["data"]["content"] == "first draft"

        # Update
        r = client.put(f"/api/notes/{report_a.report_id}", json={"content": "revised draft"})
        assert r.status_code == 200
        r = client.get(f"/api/notes/{report_a.report_id}")
        assert r.json()["data"]["content"] == "revised draft"

        # Cross-user: User B cannot read/update/delete User A's note
        current["user"] = user_b
        assert client.get(f"/api/notes/{report_a.report_id}").status_code == 404
        assert client.put(f"/api/notes/{report_a.report_id}", json={"content": "hijack"}).status_code == 404
        assert client.delete(f"/api/notes/{report_a.report_id}").status_code == 404

        # User A's note survives B's attempts
        current["user"] = user_a
        assert client.get(f"/api/notes/{report_a.report_id}").json()["data"]["content"] == "revised draft"

        # Delete
        r = client.delete(f"/api/notes/{report_a.report_id}")
        assert r.status_code == 200
        r = client.get(f"/api/notes/{report_a.report_id}")
        assert r.json()["data"]["content"] == ""  # deleted → empty, not an error
    finally:
        main_module.app.dependency_overrides.clear()


def test_notes_unauthenticated_access_rejected(api_client, repository):
    user_a = _make_user(repository, "NotesCarol")
    _, report_a = _make_report(repository, user_a["user_id"])

    assert api_client.get(f"/api/notes/{report_a.report_id}").status_code == 401
    assert api_client.put(f"/api/notes/{report_a.report_id}", json={"content": "x"}).status_code == 401
    assert api_client.delete(f"/api/notes/{report_a.report_id}").status_code == 401


def test_note_scoped_to_correct_report(api_client, repository):
    """A note on report X must not appear when looking at report Y, even
    for the same user."""
    user_a = _make_user(repository, "NotesDave")
    _, report_x = _make_report(repository, user_a["user_id"], topic="Report X")
    _, report_y = _make_report(repository, user_a["user_id"], topic="Report Y")

    import main as main_module
    from backend.auth.dependencies import get_current_user

    async def _override():
        return user_a

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        api_client.put(f"/api/notes/{report_x.report_id}", json={"content": "note for X"})
        r = api_client.get(f"/api/notes/{report_y.report_id}")
        assert r.json()["data"]["content"] == ""  # no bleed-through
    finally:
        main_module.app.dependency_overrides.clear()


# ── 2. Questions I Still Need to Answer ─────────────────────────────────────
def test_questions_derived_from_conflicting_claim(repository):
    from backend.services.questions_service import derive_unanswered_questions

    user = _make_user(repository, "QAlice")
    verification = {
        "overall_verification_score": 80.0,
        "verdict": "mostly supported",
        "claims": [
            {
                "claim_id": "claim_1",
                "claim_text": "Coffee reduces risk of X",
                "confidence": 0.7,
                "status": "conflicting",
                "evidence": [{"source_id": "src_1", "url": "https://a.com", "title": "A", "evidence_text": "...", "evidence_strength": 0.5}],
            }
        ],
    }
    _, report = _make_report(repository, user["user_id"], verification=verification)
    report_row = repository.reports.get_report(report.report_id, user_id=user["user_id"])
    sources = repository.sources.get_sources_by_report_id(report.report_id)

    questions = derive_unanswered_questions(report_row, sources)
    categories = {q["category"] for q in questions}
    assert "conflicting_evidence" in categories
    conflicting = next(q for q in questions if q["category"] == "conflicting_evidence")
    assert conflicting["related_claim_id"] == "claim_1"
    assert "Coffee reduces risk of X" in conflicting["question_text"]


def test_questions_derived_from_thin_and_undated_sources(repository):
    from backend.services.questions_service import derive_unanswered_questions

    user = _make_user(repository, "QBob")
    _, report = _make_report(
        repository,
        user["user_id"],
        sources=[
            {"url": "https://onlysource.com/a", "domain": "onlysource.com", "publish_date": None},
        ],
    )
    report_row = repository.reports.get_report(report.report_id, user_id=user["user_id"])
    sources = repository.sources.get_sources_by_report_id(report.report_id)

    questions = derive_unanswered_questions(report_row, sources)
    categories = {q["category"] for q in questions}
    assert "thin_sources" in categories
    assert "unknown_recency" in categories


def test_questions_flags_single_domain_reliance(repository):
    from backend.services.questions_service import derive_unanswered_questions

    user = _make_user(repository, "QCarol")
    _, report = _make_report(
        repository,
        user["user_id"],
        sources=[
            {"url": "https://samesite.com/a", "domain": "samesite.com", "publish_date": "2026-01-01"},
            {"url": "https://samesite.com/b", "domain": "samesite.com", "publish_date": "2026-02-01"},
        ],
    )
    report_row = repository.reports.get_report(report.report_id, user_id=user["user_id"])
    sources = repository.sources.get_sources_by_report_id(report.report_id)

    questions = derive_unanswered_questions(report_row, sources)
    single_domain = [q for q in questions if q["category"] == "single_source_domain"]
    assert len(single_domain) == 1
    assert "samesite.com" in single_domain[0]["question_text"]


def test_insufficient_research_yields_no_fabricated_questions(repository):
    """A clean report with plenty of recent, diverse sources and no
    verification data at all must not have questions invented for it —
    empty list is the correct, honest result."""
    from backend.services.questions_service import derive_unanswered_questions

    user = _make_user(repository, "QDave")
    _, report = _make_report(
        repository,
        user["user_id"],
        sources=[
            # THIN_SOURCE_COUNT_THRESHOLD is 10 (questions_service.py) — this
            # fixture must meet or exceed it to actually represent "plenty of
            # sources"; 8 sources previously used here is below the documented
            # threshold and would correctly trigger a thin_sources question,
            # which is exactly why this test was failing.
            {"url": f"https://site{i}.com/a", "domain": f"site{i}.com", "publish_date": "2026-06-01"}
            for i in range(10)
        ],
    )
    report_row = repository.reports.get_report(report.report_id, user_id=user["user_id"])
    sources = repository.sources.get_sources_by_report_id(report.report_id)

    questions = derive_unanswered_questions(report_row, sources)
    assert questions == []


def test_questions_route_requires_ownership(api_client, repository):
    user_a = _make_user(repository, "QRouteAlice")
    user_b = _make_user(repository, "QRouteBob")
    verification = {
        "overall_verification_score": 40.0,
        "verdict": "weak support",
        "claims": [
            {"claim_id": "c1", "claim_text": "Some claim", "confidence": 0.3, "status": "insufficient_evidence", "evidence": []}
        ],
    }
    _, report_a = _make_report(repository, user_a["user_id"], verification=verification)

    import main as main_module
    from backend.auth.dependencies import get_current_user

    current = {"user": user_a}

    async def _override():
        return current["user"]

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        r = api_client.get(f"/api/questions/{report_a.report_id}")
        assert r.status_code == 200
        assert len(r.json()["data"]["questions"]) >= 1

        current["user"] = user_b
        r = api_client.get(f"/api/questions/{report_a.report_id}")
        assert r.status_code == 404
    finally:
        main_module.app.dependency_overrides.clear()


def test_questions_route_unauthenticated_rejected(api_client, repository):
    user_a = _make_user(repository, "QNoAuth")
    _, report_a = _make_report(repository, user_a["user_id"])
    assert api_client.get(f"/api/questions/{report_a.report_id}").status_code == 401


def test_questions_route_404_for_nonexistent_report(api_client, repository):
    user_a = _make_user(repository, "QMissing")

    import main as main_module
    from backend.auth.dependencies import get_current_user

    async def _override():
        return user_a

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        r = api_client.get("/api/questions/report_does_not_exist_1")
        assert r.status_code == 404
    finally:
        main_module.app.dependency_overrides.clear()


# ── 3. "Research this question" ─────────────────────────────────────────────
def test_research_run_accepts_and_verifies_source_report_id(api_client, repository, monkeypatch):
    user_a = _make_user(repository, "RQAlice")
    user_b = _make_user(repository, "RQBob")
    _, report_a = _make_report(repository, user_a["user_id"], topic="Original topic")

    import backend.api.routes.research as research_route

    class _FakeGraph:
        def invoke(self, state):
            from backend.models.domain_models import ResearchReport, ResearchSession

            session = ResearchSession(topic=state["topic"], status="completed")
            report = ResearchReport(
                report_id="report_fake_followup",
                topic=state["topic"],
                summary="stub",
                report_markdown="# stub",
            )
            return {"session": session, "final_report": report, "ranked_sources": [], "verification_summary": None}

    monkeypatch.setattr(research_route, "build_research_graph", lambda: _FakeGraph())

    import main as main_module
    from backend.auth.dependencies import get_current_user

    current = {"user": user_a}

    async def _override():
        return current["user"]

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        # Owner supplying their own report_id: accepted and echoed back.
        r = api_client.post(
            "/api/research/run",
            json={"topic": "Is there stronger primary evidence for X?", "source_report_id": report_a.report_id, "persist_memory": False},
        )
        assert r.status_code == 200
        assert r.json()["data"]["source_report_id"] == report_a.report_id

        # A different user citing report_a's id: silently dropped, request
        # still succeeds (it's metadata, not a hard requirement), but
        # provenance is not falsely claimed.
        current["user"] = user_b
        r = api_client.post(
            "/api/research/run",
            json={"topic": "Is there stronger primary evidence for X?", "source_report_id": report_a.report_id, "persist_memory": False},
        )
        assert r.status_code == 200
        assert r.json()["data"]["source_report_id"] is None
    finally:
        main_module.app.dependency_overrides.clear()


def test_research_run_stays_scoped_to_authenticated_user(api_client, repository, monkeypatch):
    """The new research triggered from a question is still associated with
    whichever user actually made the request — not the source report's
    owner, and not anything client-supplied."""
    user_a = _make_user(repository, "RQCarol")

    import backend.api.routes.research as research_route

    class _FakeGraph:
        def invoke(self, state):
            from backend.models.domain_models import ResearchReport, ResearchSession

            session = ResearchSession(topic=state["topic"], status="completed")
            report = ResearchReport(report_id="report_fake_2", topic=state["topic"], summary="s", report_markdown="# s")
            return {"session": session, "final_report": report, "ranked_sources": [], "verification_summary": None}

    monkeypatch.setattr(research_route, "build_research_graph", lambda: _FakeGraph())

    import main as main_module
    from backend.auth.dependencies import get_current_user

    async def _override():
        return user_a

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        r = api_client.post("/api/research/run", json={"topic": "Follow-up research question here", "persist_memory": True})
        assert r.status_code == 200

        # The new report must show up under user_a's own history.
        r = api_client.get("/api/history/reports")
        report_ids = {item["report_id"] for item in r.json()["data"]["reports"]}
        assert "report_fake_2" in report_ids
    finally:
        main_module.app.dependency_overrides.clear()


def test_source_depth_allows_up_to_twenty(api_client, repository):
    """Validation-layer regression for the cap bump."""
    user_a = _make_user(repository, "DepthAlice")

    import main as main_module
    from backend.auth.dependencies import get_current_user

    async def _override():
        return user_a

    main_module.app.dependency_overrides[get_current_user] = _override
    try:
        import unittest.mock

        import backend.api.routes.research as research_route

        def _boom():
            raise RuntimeError("no real pipeline in this test")

        with unittest.mock.patch.object(research_route, "build_research_graph", _boom):
            r = api_client.post("/api/research/run", json={"topic": "Depth test topic", "top_k_urls": 20})
            # Passes validation (422 would mean the cap rejected it) —
            # actual pipeline failure (500, since build_research_graph is
            # stubbed to raise) is expected and irrelevant to this check.
            assert r.status_code != 422

            r = api_client.post("/api/research/run", json={"topic": "Depth test topic", "top_k_urls": 21})
            assert r.status_code == 422
    finally:
        main_module.app.dependency_overrides.clear()
