"""
API / application security hardening tests for Atlas AI (Phase 3).

Covers: production CORS lock-down, safe error handling (no leaked exception
internals), SSRF protection for the scraping path (including redirects),
practical input limits, and abuse protection (rate limits) on the
expensive endpoints. Fully offline — no Groq/Tavily/network calls; the SSRF
tests use a local aiohttp mock server, not the real internet.

Run with:
    GROQ_API_KEY=test TAVILY_API_KEY=test python -m pytest backend/tests/test_api_security.py -v
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
    yield sqlite_path, chroma_path
    config_module.get_settings.cache_clear()


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


def _make_app_client(isolated_paths):
    """Like api_client, but lets the caller set env vars (APP_ENV,
    CORS_ALLOWED_ORIGINS, SECRET_KEY, ...) before main is (re)loaded."""
    import importlib

    from backend.core import config as config_module
    config_module.get_settings.cache_clear()

    import main as main_module
    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    return TestClient(main_module.app), main_module


# ── 1. CORS ──────────────────────────────────────────────────────────────
def test_production_cors_does_not_allow_arbitrary_origins(isolated_paths, monkeypatch):
    import secrets as _secrets

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", _secrets.token_urlsafe(48))
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://myapp.example.com")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "25.00")

    client, main_module = _make_app_client(isolated_paths)
    assert main_module._cors_allow_origins == ["https://myapp.example.com"]

    with client as c:
        allowed = c.get("/health", headers={"Origin": "https://myapp.example.com"})
        blocked = c.get("/health", headers={"Origin": "https://evil.example.com"})

    assert allowed.headers.get("access-control-allow-origin") == "https://myapp.example.com"
    assert "access-control-allow-origin" not in blocked.headers


def test_production_rejects_wildcard_cors_at_startup(isolated_paths, monkeypatch):
    import secrets as _secrets

    from backend.core.config import ConfigurationError, Settings, validate_production_config

    monkeypatch.setenv("SECRET_KEY", _secrets.token_urlsafe(48))
    settings = Settings(app_env="production", secret_key=_secrets.token_urlsafe(48), cors_allowed_origins="*")
    with pytest.raises(ConfigurationError):
        validate_production_config(settings)


def test_development_cors_still_allows_localhost_convenience(api_client):
    r = api_client.get("/health", headers={"Origin": "http://localhost:5500"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://localhost:5500")


# ── 2. Safe error handling ─────────────────────────────────────────────────
def test_internal_exception_does_not_leak_details_or_query_secrets_in_production(isolated_paths, monkeypatch, caplog):
    import secrets as _secrets

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", _secrets.token_urlsafe(48))
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://myapp.example.com")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "25.00")

    client, main_module = _make_app_client(isolated_paths)

    with client as c:
        r = c.post("/api/auth/register", json={"email": "z@x.com", "name": "Z", "password": "Sup3rSecret!"})
        token = r.json()["data"]["access_token"]

        # A malformed report_id that's structurally valid but won't exist —
        # exercise a real route's exception path via a broken repository
        # call by monkeypatching it to raise, simulating an unexpected
        # internal failure (e.g. a DB error) rather than a normal 404.
        import backend.api.routes.history as history_route

        def _boom(*a, **kw):
            raise RuntimeError("connection to internal-db-host:5432 failed: password authentication failed for user 'atlas_admin'")

        original = history_route.ResearchRepository
        class _BoomRepo:
            def __init__(self):
                raise RuntimeError("connection to internal-db-host:5432 failed: password authentication failed for user 'atlas_admin'")

        history_route.ResearchRepository = _BoomRepo
        try:
            query_secret = "query-secret-should-never-be-logged"
            r = c.get(
                f"/api/history/reports?access_token={query_secret}",
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            history_route.ResearchRepository = original

    assert r.status_code == 500
    body = r.json()
    detail = str(body.get("detail", ""))
    assert "internal-db-host" not in detail
    assert "atlas_admin" not in detail
    assert "password authentication failed" not in detail
    assert "Traceback" not in detail
    assert "query-secret-should-never-be-logged" not in caplog.text
    assert "internal-db-host" not in caplog.text
    assert "password authentication failed" not in caplog.text


def test_internal_exception_detail_is_generic_in_development(api_client, monkeypatch):
    """Unexpected exception messages must never be returned to clients."""
    r = api_client.post("/api/auth/register", json={"email": "dev@x.com", "name": "D", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]

    import backend.api.routes.history as history_route

    class _BoomRepo:
        def __init__(self):
            raise RuntimeError("a specific dev-only debugging detail")

    original = history_route.ResearchRepository
    history_route.ResearchRepository = _BoomRepo
    try:
        r = api_client.get("/api/history/reports", headers={"Authorization": f"Bearer {token}"})
    finally:
        history_route.ResearchRepository = original

    assert r.status_code == 500
    assert r.json()["detail"] == "Something went wrong. Please try again."
    assert "a specific dev-only debugging detail" not in r.text


def test_validation_errors_are_still_informative(api_client):
    """Preserve useful validation errors — pydantic 422s are not the thing
    being hardened against here."""
    r = api_client.post("/api/auth/register", json={"email": "not-an-email", "name": "X", "password": "short"})
    assert r.status_code == 422
    assert r.json()["detail"]  # still has real, useful validation info


# ── 3. Security headers ────────────────────────────────────────────────────
def test_security_headers_present_on_responses(api_client):
    r = api_client.get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy")
    assert r.headers.get("content-security-policy")


def test_hsts_only_set_in_production(api_client, isolated_paths, monkeypatch):
    r = api_client.get("/health")
    assert "strict-transport-security" not in r.headers  # development

    import secrets as _secrets
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", _secrets.token_urlsafe(48))
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://myapp.example.com")
    monkeypatch.setenv("DEFAULT_MONTHLY_LLM_SPEND_CAP_USD", "25.00")
    client, _ = _make_app_client(isolated_paths)
    with client as c:
        r2 = c.get("/health")
    assert "strict-transport-security" in r2.headers


# ── 4. SSRF protection ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
async def test_ssrf_guard_blocks_internal_and_disallowed_targets(url):
    from backend.services.ssrf_guard import SSRFBlockedError, validate_url_for_fetch

    with pytest.raises(SSRFBlockedError):
        await validate_url_for_fetch(url)


async def test_ssrf_guard_allows_public_looking_hosts():
    """Uses a hostname that resolves via /etc/hosts-independent public DNS
    semantics — mocked here by monkeypatching getaddrinfo to a known public
    IP, so this test doesn't depend on real network access."""
    import asyncio

    from backend.services.ssrf_guard import validate_url_for_fetch

    async def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]  # example.com's old public IP

    loop = asyncio.get_event_loop()
    original = loop.getaddrinfo
    loop.getaddrinfo = fake_getaddrinfo
    try:
        await validate_url_for_fetch("https://example.com/page")  # must not raise
    finally:
        loop.getaddrinfo = original


async def test_ssrf_guard_blocks_redirect_to_internal_target():
    """The core "redirect" requirement: a URL that resolves to a public
    address must still be blocked if it redirects to an internal one."""
    import aiohttp
    from aiohttp import web

    from backend.services.ssrf_guard import SSRFBlockedError, safe_get

    async def redirect_to_metadata(request):
        return web.Response(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    app = web.Application()
    app.router.add_get("/redirect", redirect_to_metadata)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(SSRFBlockedError):
                async with safe_get(session, f"http://127.0.0.1:{port}/redirect"):
                    pass
    finally:
        await runner.cleanup()


async def test_ssrf_guard_follows_legitimate_redirects():
    """A same-host redirect to another public-looking path must still work
    — SSRF protection shouldn't break normal web research."""
    import ipaddress

    import aiohttp
    from aiohttp import web

    import backend.services.ssrf_guard as ssrf_guard
    from backend.services.ssrf_guard import safe_get

    # Isolate: allow loopback through for this test only, so a local mock
    # server can stand in for "a normal public site that redirects once".
    # SSRF-blocking of loopback/private/metadata targets is covered by the
    # tests above and is not what's under test here.
    original = ssrf_guard._is_blocked_ip

    def patched(ip):
        if ip.is_loopback:
            return False
        return original(ip)

    ssrf_guard._is_blocked_ip = patched

    async def normal_page(request):
        return web.Response(status=200, text="ok")

    async def redirect_once(request):
        return web.Response(status=302, headers={"Location": "/normal"})

    app = web.Application()
    app.router.add_get("/normal", normal_page)
    app.router.add_get("/redirect-once", redirect_once)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as session:
            async with safe_get(session, f"http://127.0.0.1:{port}/redirect-once") as resp:
                assert resp.status == 200
                assert await resp.text() == "ok"
    finally:
        ssrf_guard._is_blocked_ip = original
        await runner.cleanup()


# ── 5. Input limits ─────────────────────────────────────────────────────────
def test_oversized_request_body_rejected(api_client):
    big_body = "x" * (3 * 1024 * 1024)  # bigger than the 2MB default ceiling
    r = api_client.post(
        "/api/auth/forgot-password",
        content=big_body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_oversized_chat_history_rejected(api_client):
    api_client.post("/api/auth/register", json={"email": "hist@x.com", "name": "H", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "hist@x.com", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]

    oversized_history = [{"role": "user", "content": "hi"} for _ in range(1000)]
    r = api_client.post(
        "/api/chat/research",
        json={"topic": "Some topic here", "question": "A question?", "history": oversized_history},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_oversized_research_topic_rejected(api_client):
    api_client.post("/api/auth/register", json={"email": "topic@x.com", "name": "T", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "topic@x.com", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]

    r = api_client.post(
        "/api/research/run",
        json={"topic": "x" * 5000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ── 6. Abuse protection on expensive endpoints ──────────────────────────────
def test_research_run_endpoint_is_rate_limited(api_client, monkeypatch):
    api_client.post("/api/auth/register", json={"email": "researcher@x.com", "name": "R", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "researcher@x.com", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Force every call to fail fast at the graph-build step rather than
    # actually running the research pipeline (which needs real Groq/Tavily
    # access) — only the rate-limit dependency (which runs before the route
    # body) is under test here.
    import backend.api.routes.research as research_route

    def _boom():
        raise RuntimeError("no LLM access in this test")

    monkeypatch.setattr(research_route, "build_research_graph", _boom)

    statuses = []
    for _ in range(8):
        r = api_client.post("/api/research/run", json={"topic": "Rate limit test topic"}, headers=headers)
        statuses.append(r.status_code)

    assert 429 in statuses, f"expected a 429 after repeated research requests, got {statuses}"


def test_semantic_search_endpoint_is_rate_limited(api_client, monkeypatch):
    api_client.post("/api/auth/register", json={"email": "searcher@x.com", "name": "S", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "searcher@x.com", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from backend.memory.embedding_service import EmbeddingService
    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, text: [0.0] * 16)

    statuses = []
    for _ in range(35):
        r = api_client.post("/api/search/semantic", json={"query": "rate limit probe"}, headers=headers)
        statuses.append(r.status_code)

    assert 429 in statuses, f"expected a 429 after repeated search requests, got {statuses}"


def test_rate_limits_are_per_user_not_shared_globally(api_client, monkeypatch):
    """Two different users hitting the same expensive endpoint must not
    share one limit bucket keyed by IP alone."""
    from backend.memory.embedding_service import EmbeddingService
    monkeypatch.setattr(EmbeddingService, "embed_text", lambda self, text: [0.0] * 16)

    api_client.post("/api/auth/register", json={"email": "userA@x.com", "name": "A", "password": "Sup3rSecret!"})
    api_client.post("/api/auth/register", json={"email": "userB@x.com", "name": "B", "password": "Sup3rSecret!"})
    token_a = api_client.post("/api/auth/login", json={"email": "userA@x.com", "password": "Sup3rSecret!"}).json()["data"]["access_token"]
    token_b = api_client.post("/api/auth/login", json={"email": "userB@x.com", "password": "Sup3rSecret!"}).json()["data"]["access_token"]

    # Exhaust user A's limit.
    for _ in range(30):
        api_client.post("/api/search/semantic", json={"query": "probe"}, headers={"Authorization": f"Bearer {token_a}"})
    r_a = api_client.post("/api/search/semantic", json={"query": "probe"}, headers={"Authorization": f"Bearer {token_a}"})
    assert r_a.status_code == 429

    # User B, same process/IP, should be unaffected.
    r_b = api_client.post("/api/search/semantic", json={"query": "probe"}, headers={"Authorization": f"Bearer {token_b}"})
    assert r_b.status_code == 200
