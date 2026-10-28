"""Stable, content-addressed IDs for the knowledge graph.

IDs are a pure function of ``(namespace, normalised content)``: the same URL /
passage / claim / entity always yields the same ID, so repeated investigations
upsert instead of duplicating. The *namespace* is the owning user's id, which
(a) makes cross-tenant collisions impossible and (b) means one user's IDs never
reveal that another user holds the same source.

Prefixes: ``src_`` Source, ``evd_`` Evidence, ``clm_`` Claim, ``ent_`` Entity.
Bump ``ID_VERSION`` only with a deliberate migration — it changes every ID.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ID_VERSION = 1
_TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "_hsenc", "_hsmi"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_text(text: str | None) -> str:
    """Case-fold, drop edge punctuation, collapse whitespace."""
    collapsed = " ".join((text or "").lower().split())
    return re.sub(r"^[\W_]+|[\W_]+$", "", collapsed)


def canonicalize_url(url: str) -> str:
    """Canonical form: https, lowercase host without ``www.``/default port, no
    fragment, tracking parameters removed, query sorted, no trailing slash."""
    parts = urlsplit((url or "").strip())
    if not parts.hostname:
        raise ValueError(f"Not an absolute URL: {url!r}")
    host = parts.hostname.lower().removeprefix("www.")
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS.get(parts.scheme.lower())) else f"{host}:{port}"
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    ))
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("https", netloc, path, query, ""))


def _digest(namespace: str, kind: str, *parts: str) -> str:
    if not namespace:
        raise ValueError("A non-empty namespace (user id) is required for knowledge IDs")
    raw = "\x1f".join([f"atlas.knowledge.v{ID_VERSION}", namespace, kind, *parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def source_id(namespace: str, url: str) -> str:
    return "src_" + _digest(namespace, "source", canonicalize_url(url))


def evidence_id(namespace: str, source_id_: str, passage: str) -> str:
    norm = normalize_text(passage)
    if not norm:
        raise ValueError("Evidence passage must not be empty")
    return "evd_" + _digest(namespace, "evidence", source_id_, norm)


def claim_id(namespace: str, claim_text: str) -> str:
    norm = normalize_text(claim_text)
    if not norm:
        raise ValueError("Claim text must not be empty")
    return "clm_" + _digest(namespace, "claim", norm)


def entity_id(namespace: str, name: str) -> str:
    """Type is deliberately NOT part of the ID: type labels are heuristic and
    mutable, and must not fork one entity into two."""
    norm = normalize_text(name)
    if not norm:
        raise ValueError("Entity name must not be empty")
    return "ent_" + _digest(namespace, "entity", norm)
