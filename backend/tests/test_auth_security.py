"""
Authentication / account-security tests for Atlas AI (Phase 2).

Covers: Argon2id password hashing + legacy PBKDF2 compatibility/upgrade,
signed-token validation (valid/expired/bad-signature/malformed/missing),
production secret-safety checks, auth-endpoint rate limiting, and the new
forgot/reset-password flow. Fully offline — no Groq/Tavily/network calls.

Run with:
    GROQ_API_KEY=test TAVILY_API_KEY=test python -m pytest backend/tests/test_auth_security.py -v
"""
from __future__ import annotations

import os
import time

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
    importlib.reload(main_module)  # fresh app + fresh lru_cache()s per test

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as client:
        yield client


# ── 1. Password hashing ──────────────────────────────────────────────────
def test_correct_password_login_succeeds(api_client):
    api_client.post("/api/auth/register", json={"email": "a@x.com", "name": "A", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "a@x.com", "password": "Sup3rSecret!"})
    assert r.status_code == 200
    assert r.json()["data"]["access_token"]


def test_incorrect_password_login_fails(api_client):
    api_client.post("/api/auth/register", json={"email": "b@x.com", "name": "B", "password": "Sup3rSecret!"})
    r = api_client.post("/api/auth/login", json={"email": "b@x.com", "password": "WrongPassword1"})
    assert r.status_code == 401


def test_password_hash_is_not_plaintext_and_is_argon2id(repository):
    from backend.auth.security import hash_password

    raw_password = "Sup3rSecret!"
    password_hash, salt = hash_password(raw_password)

    assert password_hash != raw_password
    assert raw_password not in password_hash
    assert password_hash.startswith("$argon2id$")

    user = repository.users.create(
        email="c@x.com", name="C", password_hash=password_hash, password_salt=salt
    )
    stored = repository.users.get_by_id(user["user_id"])
    assert stored["password_hash"] != raw_password
    assert stored["password_hash"].startswith("$argon2id$")


def test_legacy_pbkdf2_hash_still_verifies_and_upgrades_on_login(repository, api_client):
    """Simulates an account created before Argon2id existed: a raw PBKDF2
    hash stored directly (bypassing hash_password, which now only produces
    Argon2id). It must still be able to log in, and the stored hash must be
    replaced with Argon2id immediately after."""
    import base64
    import hashlib

    from backend.auth.security import PBKDF2_ITERATIONS, needs_rehash, verify_password

    password = "LegacyPassw0rd!"
    salt = base64.urlsafe_b64encode(os.urandom(16)).decode()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    legacy_hash = base64.urlsafe_b64encode(digest).decode()

    assert verify_password(password, legacy_hash, salt) is True
    assert needs_rehash(legacy_hash) is True

    user = repository.users.create(email="legacy@x.com", name="Legacy", password_hash=legacy_hash, password_salt=salt)

    r = api_client.post("/api/auth/login", json={"email": "legacy@x.com", "password": password})
    assert r.status_code == 200

    upgraded = repository.users.get_by_id(user["user_id"])
    assert upgraded["password_hash"].startswith("$argon2id$")
    assert upgraded["password_hash"] != legacy_hash

    # And it still logs in fine on the new hash.
    r = api_client.post("/api/auth/login", json={"email": "legacy@x.com", "password": password})
    assert r.status_code == 200


# ── 2. Token validation ──────────────────────────────────────────────────
def test_valid_token_works(api_client):
    r = api_client.post("/api/auth/register", json={"email": "d@x.com", "name": "D", "password": "Sup3rSecret!"})
    token = r.json()["data"]["access_token"]
    r = api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_expired_token_rejected(repository):
    from backend.auth.security import create_access_token, decode_access_token

    user = repository.users.create(email="e@x.com", name="E", password_hash="x", password_salt="")
    token = create_access_token(user_id=user["user_id"], email=user["email"], expires_minutes=-1)
    with pytest.raises(ValueError, match="expired"):
        decode_access_token(token)


def test_invalid_signature_rejected():
    from backend.auth.security import create_access_token, decode_access_token

    token = create_access_token(user_id="user_x", email="x@x.com")
    payload_b64, _sig = token.split(".")
    tampered = f"{payload_b64}.not-a-real-signature"
    with pytest.raises(ValueError, match="signature"):
        decode_access_token(tampered)


@pytest.mark.parametrize(
    "bad_token",
    [
        "not-a-token-at-all",
        "onlyonesegment",
        "a.b.c",
        "",
        "..",
    ],
)
def test_malformed_token_rejected(bad_token):
    from backend.auth.security import decode_access_token

    with pytest.raises(ValueError):
        decode_access_token(bad_token)


def test_protected_endpoint_without_token_rejected(api_client):
    r = api_client.get("/api/auth/me")
    assert r.status_code == 401
    r = api_client.get("/api/history/reports")
    assert r.status_code == 401


# ── 3. Production secret safety ──────────────────────────────────────────
def test_production_config_rejects_default_secret(isolated_paths, monkeypatch):
    from backend.core.config import ConfigurationError, Settings, validate_production_config

    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings(secret_key="dev-insecure-secret-change-me-in-.env")
    with pytest.raises(ConfigurationError):
        validate_production_config(settings)


@pytest.mark.parametrize("secret", [
    "replace-with-a-random-secret-of-at-least-32-characters",
    "dev-insecure-secret-change-me-in-.env",
    "your-secret-key-that-is-long-enough-to-pass-length-check",
    "example-secret-key-that-is-long-enough-to-pass-length-check",
    "x" * 31,
    "a" * 48,
    "abc123" * 8,
    "0123456789" * 5,
    "abcdefghijklmnopqrstuvwxyz0123456789",
    "password1234567890123456789012345678",
])
def test_production_config_rejects_example_and_weak_keys(isolated_paths, secret):
    from backend.core.config import ConfigurationError, Settings, validate_production_config

    settings = Settings(app_env="production", secret_key=secret,
                        cors_allowed_origins="https://atlas.example.com")
    with pytest.raises(ConfigurationError):
        validate_production_config(settings)


def test_env_example_secret_is_rejected_for_production(isolated_paths):
    from pathlib import Path
    from backend.core.config import ConfigurationError, Settings, validate_production_config

    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    key = next(line.split("=", 1)[1] for line in example.splitlines() if line.startswith("SECRET_KEY="))
    with pytest.raises(ConfigurationError):
        validate_production_config(Settings(app_env="production", secret_key=key,
                                            cors_allowed_origins="https://atlas.example.com"))


def test_production_config_rejects_short_secret(isolated_paths):
    from backend.core.config import ConfigurationError, Settings, validate_production_config

    settings = Settings(app_env="production", secret_key="short")
    with pytest.raises(ConfigurationError):
        validate_production_config(settings)


def test_production_config_accepts_strong_secret(isolated_paths):
    import secrets as _secrets

    from backend.core.config import Settings, validate_production_config

    settings = Settings(app_env="production", secret_key=_secrets.token_urlsafe(48),
                        cors_allowed_origins="https://atlas.example.com,https://www.atlas.example.com",
                        default_monthly_llm_spend_cap_usd=25.0)
    validate_production_config(settings)  # must not raise


def test_production_config_requires_explicit_monthly_spend_cap(isolated_paths):
    import secrets as _secrets

    from backend.core.config import ConfigurationError, Settings, validate_production_config

    settings = Settings(app_env="production", secret_key=_secrets.token_urlsafe(48),
                        cors_allowed_origins="https://atlas.example.com")
    with pytest.raises(ConfigurationError, match="DEFAULT_MONTHLY_LLM_SPEND_CAP_USD"):
        validate_production_config(settings)


def test_production_config_accepts_configured_monthly_spend_cap(isolated_paths):
    import secrets as _secrets

    from backend.core.config import Settings, validate_production_config

    settings = Settings(app_env="production", secret_key=_secrets.token_urlsafe(48),
                        cors_allowed_origins="https://atlas.example.com",
                        default_monthly_llm_spend_cap_usd=25.0)
    validate_production_config(settings)


def test_development_config_allows_default_secret(isolated_paths):
    from backend.core.config import Settings, validate_production_config

    settings = Settings(app_env="development", secret_key="dev-insecure-secret-change-me-in-.env")
    validate_production_config(settings)  # must not raise — dev stays easy to run


# ── 4. Rate limiting ──────────────────────────────────────────────────────
def test_login_endpoint_is_rate_limited(api_client):
    api_client.post("/api/auth/register", json={"email": "f@x.com", "name": "F", "password": "Sup3rSecret!"})

    statuses = []
    for _ in range(15):
        r = api_client.post("/api/auth/login", json={"email": "f@x.com", "password": "WrongPassword1"})
        statuses.append(r.status_code)

    assert 429 in statuses, f"expected a 429 somewhere after repeated attempts, got {statuses}"
    assert statuses[0] == 401  # legitimate wrong-password attempts still work until the limit trips


def test_register_endpoint_is_rate_limited(api_client):
    statuses = []
    for i in range(10):
        r = api_client.post(
            "/api/auth/register",
            json={"email": f"spam{i}@x.com", "name": "Spam", "password": "Sup3rSecret!"},
        )
        statuses.append(r.status_code)

    assert 429 in statuses, f"expected a 429 after repeated registrations, got {statuses}"


# ── 5. Password reset ─────────────────────────────────────────────────────
def test_forgot_password_generic_response_for_existing_and_unknown_email(api_client):
    api_client.post("/api/auth/register", json={"email": "g@x.com", "name": "G", "password": "Sup3rSecret!"})

    r1 = api_client.post("/api/auth/forgot-password", json={"email": "g@x.com"})
    r2 = api_client.post("/api/auth/forgot-password", json={"email": "does-not-exist@x.com"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["message"] == r2.json()["message"]


def _extract_dev_reset_token(forgot_password_response) -> str:
    """Phase 3 hardening: the reset token is never written to logs (log
    files are routinely shipped/retained far more widely than a single API
    response). In development only, it's returned directly in the
    forgot-password response instead — see backend/api/routes/auth.py."""
    token = forgot_password_response.json()["data"]["dev_reset_token"]
    assert token, "expected dev_reset_token in the response (development app_env)"
    return token


def test_forgot_password_never_logs_the_raw_token(api_client, caplog):
    import logging

    api_client.post("/api/auth/register", json={"email": "lognotoken@x.com", "name": "L", "password": "Sup3rSecret!"})
    with caplog.at_level(logging.INFO):
        r = api_client.post("/api/auth/forgot-password", json={"email": "lognotoken@x.com"})

    raw_token = r.json()["data"]["dev_reset_token"]
    assert raw_token

    for record in caplog.records:
        assert raw_token not in record.getMessage()
        for value in record.__dict__.values():
            assert raw_token not in str(value), "raw reset token must never appear in logs"


def test_reset_password_with_valid_token_changes_password(api_client, repository):
    api_client.post("/api/auth/register", json={"email": "h@x.com", "name": "H", "password": "OldPassw0rd!"})

    forgot_resp = api_client.post("/api/auth/forgot-password", json={"email": "h@x.com"})
    raw_token = _extract_dev_reset_token(forgot_resp)

    r = api_client.post("/api/auth/reset-password", json={"token": raw_token, "new_password": "NewPassw0rd!"})
    assert r.status_code == 200

    # Old password no longer works, new one does.
    r = api_client.post("/api/auth/login", json={"email": "h@x.com", "password": "OldPassw0rd!"})
    assert r.status_code == 401
    r = api_client.post("/api/auth/login", json={"email": "h@x.com", "password": "NewPassw0rd!"})
    assert r.status_code == 200


def test_reset_token_is_single_use(api_client):
    api_client.post("/api/auth/register", json={"email": "i@x.com", "name": "I", "password": "OldPassw0rd!"})
    forgot_resp = api_client.post("/api/auth/forgot-password", json={"email": "i@x.com"})
    raw_token = _extract_dev_reset_token(forgot_resp)

    r1 = api_client.post("/api/auth/reset-password", json={"token": raw_token, "new_password": "FirstNew1!"})
    assert r1.status_code == 200

    r2 = api_client.post("/api/auth/reset-password", json={"token": raw_token, "new_password": "SecondNew1!"})
    assert r2.status_code == 400


def test_reset_password_invalid_token_rejected(api_client):
    r = api_client.post("/api/auth/reset-password", json={"token": "not-a-real-token", "new_password": "Whatever1!"})
    assert r.status_code == 400


def test_reset_password_expired_token_rejected(api_client, repository):
    from backend.auth.security import generate_reset_token, hash_reset_token

    user = repository.users.create(email="j@x.com", name="J", password_hash="x", password_salt="")
    raw_token = generate_reset_token()
    # expires_minutes=-1 → already in the past.
    repository.password_resets.create(user_id=user["user_id"], token_hash=hash_reset_token(raw_token), expires_minutes=-1)

    r = api_client.post("/api/auth/reset-password", json={"token": raw_token, "new_password": "Whatever1!"})
    assert r.status_code == 410


def test_reset_token_not_stored_raw(repository):
    from backend.auth.security import generate_reset_token, hash_reset_token

    user = repository.users.create(email="k@x.com", name="K", password_hash="x", password_salt="")
    raw_token = generate_reset_token()
    repository.password_resets.create(user_id=user["user_id"], token_hash=hash_reset_token(raw_token), expires_minutes=30)

    record = repository.password_resets.get_by_hash(hash_reset_token(raw_token))
    assert record is not None
    assert record["token_hash"] != raw_token
    assert raw_token not in record.values()
