"""Password-reset email through Brevo's HTTPS API.

The HTTPS API is used instead of SMTP because some free hosts block outbound SMTP ports.
Sending is optional: without BREVO_API_KEY and BREVO_SENDER_EMAIL nothing is sent.
The reset link and the key are never logged.
"""
from __future__ import annotations

import html
import os
from urllib.parse import quote

import httpx

from backend.core.logging import get_logger

logger = get_logger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def email_configured() -> bool:
    return bool(os.environ.get("BREVO_API_KEY", "").strip() and os.environ.get("BREVO_SENDER_EMAIL", "").strip())


def reset_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/auth.html?reset_token={quote(token)}"


def send_password_reset(to_email: str, name: str, link: str, expires_minutes: int) -> bool:
    """Send the reset email. Returns False (and logs why, without secrets) instead of raising."""
    if not email_configured():
        return False
    safe_name, safe_link = html.escape(name or "there"), html.escape(link, quote=True)
    payload = {
        "sender": {
            "email": os.environ["BREVO_SENDER_EMAIL"].strip(),
            "name": os.environ.get("BREVO_SENDER_NAME", "").strip() or "Atlas",
        },
        "to": [{"email": to_email}],
        "subject": "Reset your Atlas password",
        "htmlContent": (
            f"<p>Hello {safe_name},</p>"
            f'<p><a href="{safe_link}">Set a new Atlas password</a></p>'
            f"<p>The link expires in {int(expires_minutes)} minutes and works once. "
            "If you did not ask for this, you can ignore this email.</p>"
        ),
    }
    try:
        response = httpx.post(
            BREVO_URL, json=payload, timeout=10,
            headers={"api-key": os.environ["BREVO_API_KEY"].strip(), "accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.warning("Password reset email not sent", extra={"error_type": type(exc).__name__})
        return False
    if response.status_code >= 300:
        # 401 usually means an SMTP key (xsmtpsib-) was used where an API key (xkeysib-) is needed.
        logger.warning("Password reset email rejected by Brevo", extra={"status_code": response.status_code})
        return False
    logger.info("Password reset email accepted by Brevo")
    return True
