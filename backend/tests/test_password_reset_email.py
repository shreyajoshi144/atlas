"""Password-reset email: sent only when configured, never leaks the token, never reveals accounts."""
from __future__ import annotations

import uuid

import httpx
from fastapi.testclient import TestClient

from backend.services import email_service


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _configure(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test-value")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("BREVO_SENDER_NAME", "Atlas")


def test_nothing_is_sent_without_configuration(monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")))
    assert email_service.send_password_reset("a@example.com", "A", "https://x/auth.html?reset_token=t", 30) is False


def test_payload_escapes_the_name_and_uses_the_sender(monkeypatch):
    _configure(monkeypatch)
    sent = {}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.update(url=url, **kw) or _Response(201))
    assert email_service.send_password_reset("a@example.com", "<b>x</b>", email_service.reset_link("https://h/", "tok"), 30)
    assert sent["url"] == email_service.BREVO_URL
    assert sent["json"]["sender"] == {"email": "sender@example.com", "name": "Atlas"}
    assert "<b>x</b>" not in sent["json"]["htmlContent"]
    assert "https://h/auth.html?reset_token=tok" in sent["json"]["htmlContent"]


def test_rejection_and_network_errors_do_not_raise(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(401))
    assert email_service.send_password_reset("a@example.com", "A", "https://h", 30) is False

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    assert email_service.send_password_reset("a@example.com", "A", "https://h", 30) is False


def test_forgot_password_emails_only_real_accounts_with_the_same_reply(monkeypatch):
    import main

    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(kw["json"]) or _Response(201))
    email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
    with TestClient(main.app) as client:
        client.post("/api/auth/register", json={"email": email, "name": "R", "password": "Test-pass-12345"})
        known = client.post("/api/auth/forgot-password", json={"email": email}).json()
        unknown = client.post("/api/auth/forgot-password", json={"email": "nobody-" + email}).json()
    assert known["message"] == unknown["message"]
    assert [c["to"][0]["email"] for c in calls] == [email]
