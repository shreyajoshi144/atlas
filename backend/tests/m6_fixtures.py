"""Shared M6 test scaffolding: builds multi-investigation knowledge graphs with
controllable investigation times, publish dates and content hashes.

Import into a test module with
``from backend.tests.m6_fixtures import *  # noqa: F401,F403``.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

import pytest

from backend.knowledge.map_service import KnowledgeMapService
from backend.knowledge.service import KnowledgeService
from backend.models.domain_models import (
    ClaimEvidence, RankedSource, ResearchReport, ResearchSession, VerifiedClaim,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T1 = "2026-01-10T09:00:00+00:00"
T2 = "2026-06-01T09:00:00+00:00"
T3 = "2026-09-20T09:00:00+00:00"

S1 = "https://a.example.com/solar"
S2 = "https://b.example.org/wind"
S3 = "https://c.example.net/grid"


def _fake_vector(text: str, dim: int = 16) -> list[float]:
    return [b / 255.0 for b in hashlib.sha256(text.encode()).digest()[:dim]]


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    from backend.memory.embedding_service import EmbeddingService

    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, t: _fake_vector(t))
    monkeypatch.setattr(EmbeddingService, "embed_texts", lambda self, ts: [_fake_vector(t) for t in ts])


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    from backend.core import config as config_module
    from backend.database import ResearchRepository

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "atlas.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    config_module.get_settings.cache_clear()
    repo = ResearchRepository()
    repo.initialize()
    yield repo
    config_module.get_settings.cache_clear()


@pytest.fixture()
def svc(repository):
    return KnowledgeMapService(repository, clock=lambda: NOW)


def make_user(repository, name: str) -> str:
    from backend.auth.security import hash_password

    h, salt = hash_password("Sup3rSecret!")
    return repository.users.create(email=f"{name.lower()}@example.com", name=name, password_hash=h, password_salt=salt)["user_id"]


def src(url: str, published: str | None = None, *, hash: str | None = None, title: str = "T") -> dict:
    return {"url": url, "published": published, "hash": hash, "title": title}


def claim(text: str, status: str = "verified", confidence: float = 0.8, evidence=()):
    """evidence: iterable of (url, passage, stance, strength)."""
    return {"text": text, "status": status, "confidence": confidence, "evidence": list(evidence)}


def investigate(repository, user_id: str, topic: str, when: str, sources: list[dict], claims: list[dict]) -> str:
    """Create a report at time ``when`` and record its knowledge. Returns report_id."""
    topic_id = repository.topics.get_or_create_topic(topic, user_id=user_id)
    session = ResearchSession(topic=topic, status="completed")
    repository.sessions.save_session(topic_id, session, user_id=user_id)
    report = ResearchReport(report_id=f"report_{uuid.uuid4().hex[:12]}", topic=topic, summary="s",
                            report_markdown="body", key_statistics=[], swot={})
    repository.reports.save_report(topic_id, session, report)
    with repository.db.get_connection() as conn:  # pin the investigation time
        conn.execute("UPDATE reports SET created_at = ?, updated_at = ? WHERE report_id = ?", (when, when, report.report_id))
        conn.commit()
    ranked = [
        RankedSource(title=s["title"], url=s["url"], domain=s["url"].split("/")[2], snippet="s", rank_position=i + 1,
                     publish_date=date.fromisoformat(s["published"]) if s["published"] else None)
        for i, s in enumerate(sources)
    ]
    verified = [
        VerifiedClaim(
            claim_text=c["text"], confidence=c["confidence"], status=c["status"],
            evidence=[ClaimEvidence(source_id="", url=u, title="t", evidence_text=p, stance=st, evidence_strength=sg)
                      for (u, p, st, sg) in c["evidence"]],
        )
        for c in claims
    ]
    scraped = [{"url": s["url"], "content_hash": s["hash"]} for s in sources if s["hash"]]
    KnowledgeService(repository).record_investigation(user_id, report.report_id, ranked, verified, scraped_contents=scraped)
    return report.report_id


def evolving_scenario(repository, user_id: str) -> dict:
    """Two investigations of the same topic exercising every change class."""
    r1 = investigate(
        repository, user_id, "Energy", T1,
        [src(S1, "2026-05-20", hash="h1"), src(S2, "2024-01-01", hash="h2"), src(S3, "2026-08-01", hash="h3")],
        [
            claim("Solar capacity grew in Europe", "partially_verified", 0.5, [(S1, "Reports say Solar capacity grew in Europe.", "support", 0.5)]),
            claim("Wind output doubled", "verified", 0.9, [(S2, "Reports say Wind output doubled.", "support", 0.9)]),
            claim("Grid storage is cheap", "verified", 0.8, [(S3, "Reports say Grid storage is cheap.", "support", 0.8)]),
            claim("Hydro is stable", "verified", 0.8, [(S1, "Reports say Hydro is stable.", "support", 0.8)]),
            claim("Tidal pilot launched", "verified", 0.7, [(S2, "Reports say Tidal pilot launched.", "support", 0.7)]),
        ],
    )
    r2 = investigate(
        repository, user_id, "Energy", T2,
        [src(S1, "2026-05-20", hash="h1b"), src(S2, "2024-01-01", hash="h2b"), src(S3, "2026-08-01", hash="h3")],
        [
            claim("Solar capacity grew in Europe", "verified", 0.9, [
                (S1, "Reports say Solar capacity grew in Europe.", "support", 0.9),
                (S3, "Analysts say Solar capacity grew in Europe.", "support", 0.8)]),
            claim("Wind output doubled", "partially_verified", 0.4, [(S2, "Reports say Wind output doubled.", "support", 0.4)]),
            claim("Grid storage is cheap", "conflicting", 0.3, [(S3, "Reports say Grid storage is cheap.", "contradict", 0.7)]),
            claim("Hydro is stable", "verified", 0.8, [(S1, "Reports say Hydro is stable.", "support", 0.8)]),
            claim("Offshore wind permits rose", "verified", 0.8, [(S3, "Reports say Offshore wind permits rose.", "support", 0.8)]),
        ],
    )
    return {"r1": r1, "r2": r2}
