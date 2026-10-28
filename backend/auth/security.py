from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

from backend.core.config import get_settings

# Legacy PBKDF2 parameters — kept only so password hashes created before
# Argon2id was introduced can still be verified. Never used for new hashes.
PBKDF2_ITERATIONS = 200_000

# Argon2id is the OWASP-recommended default for new password hashes:
# memory-hard (resists GPU/ASIC cracking) and self-describing (the encoded
# hash string carries its own salt + parameters, so no separate salt column
# is needed for new accounts).
_argon2_hasher = PasswordHasher()


def _legacy_pbkdf2_hash(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(digest).decode()


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash, salt). Every password hashed from here on is
    Argon2id — salt is returned as "" (kept only for signature/schema
    compatibility with UserRepository.create/update_password_hash, which
    still has a password_salt column for legacy PBKDF2 rows). Argon2id
    embeds its own random salt inside the encoded hash string itself, so
    nothing is lost by not using that column for new hashes."""
    return _argon2_hasher.hash(password), ""


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Verifies against either hash format so existing accounts are never
    locked out. Argon2id hashes are self-describing (always start with
    $argon2); anything else is treated as a legacy PBKDF2 hash paired with
    the stored salt. Callers should follow a successful verify against a
    legacy hash with needs_rehash()+hash_password() to upgrade it."""
    if password_hash.startswith("$argon2"):
        try:
            return _argon2_hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False
        except InvalidHash:
            return False

    candidate_hash = _legacy_pbkdf2_hash(password, salt)
    return hmac.compare_digest(candidate_hash, password_hash)


def needs_rehash(password_hash: str) -> bool:
    """True for legacy PBKDF2 hashes (always — they should migrate the
    first chance they get), or for an Argon2id hash whose parameters are
    weaker than this app's current target (e.g. after PasswordHasher()'s
    defaults or an explicit override are bumped later)."""
    if not password_hash.startswith("$argon2"):
        return True
    try:
        return _argon2_hasher.check_needs_rehash(password_hash)
    except InvalidHash:
        return True


def generate_reset_token() -> str:
    """A cryptographically secure, single-use, opaque password-reset token.
    Only its hash (see hash_reset_token) is ever persisted — this raw value
    exists only in memory and in the one-time reset link."""
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    """SHA-256 is intentionally used here instead of Argon2id: this hashes
    a high-entropy random token (not a low-entropy user-chosen password),
    so a fast, deterministic hash is correct — it just needs to make the
    stored value non-reversible and support an exact-match DB lookup by
    hash. Argon2id's slow, salted design solves a different problem
    (resisting offline guessing of low-entropy secrets) and would only add
    unnecessary latency here."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(user_id: str, email: str, expires_minutes: int | None = None) -> str:
    """
    A minimal, dependency-free JWT-like signed token: base64url(payload) + '.' + HMAC-SHA256 signature.
    Not a spec-compliant JWT, but the same security property (tamper-evident, server-signed, expiring).
    """
    settings = get_settings()
    minutes = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes

    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + minutes * 60,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    signature_b64 = _b64url_encode(signature)
    return f"{payload_b64}.{signature_b64}"


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()

    try:
        payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise ValueError("Malformed token") from exc

    expected_signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_signature_b64 = _b64url_encode(expected_signature)

    if not hmac.compare_digest(expected_signature_b64, signature_b64):
        raise ValueError("Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed token payload") from exc

    if not isinstance(payload, dict):
        raise ValueError("Malformed token payload")

    if payload.get("exp", 0) < time.time():
        raise ValueError("Token has expired")

    return payload
