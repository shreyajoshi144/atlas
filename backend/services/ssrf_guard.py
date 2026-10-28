"""
SSRF (server-side request forgery) protection for Atlas's web-scraping path.

Atlas fetches URLs returned by Tavily's search API and scrapes their HTML.
Those URLs are external and not typed directly by a user, but they're still
untrusted input from the app's point of view — a malicious or compromised
search result (or a manipulated ranking pipeline) could point at an internal
address, and nothing before this module stopped that. This guard is applied
to every outbound fetch in AsyncScrapingService, including each redirect hop.

What this protects against: the scraper being used to reach localhost,
private/link-local/reserved IP ranges, or cloud metadata endpoints (e.g.
169.254.169.254) — either directly or via an HTTP redirect from an
otherwise-public-looking URL.

What this does NOT fully protect against (documented, not silently ignored):
DNS rebinding — the IP is validated at request time via getaddrinfo(), but
the connection itself is opened by aiohttp against the hostname again, so a
DNS record that changes between the check and the connection could in
principle race past this. Closing that fully would mean pinning the
validated IP for the actual socket connection (a custom resolver/connector),
which is a larger change than this phase's "keep changes minimal" scope
calls for. Blocking the entire private/reserved IP space, redirects
included, is the high-value part of SSRF protection and is what's
implemented here.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from backend.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES = {"http", "https"}

# Resolving to any of these hostnames is blocked outright, independent of
# whatever IP they resolve to today — catches the common cloud-metadata
# hostnames directly, on top of the IP-range checks below.
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
}


class SSRFBlockedError(ValueError):
    """Raised when a URL (or a redirect target) resolves to a blocked
    destination. Treated as a scrape failure by the caller, not a crash."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def validate_url_for_fetch(url: str) -> None:
    """Raises SSRFBlockedError if `url` must not be fetched. Called before
    the initial request and again before following each redirect hop."""
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"Blocked URL scheme: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SSRFBlockedError("Credentials in URLs are not allowed")

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise SSRFBlockedError(f"Blocked hostname: {hostname}")

    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"DNS resolution failed for host: {hostname}") from exc

    resolved_ips = {info[4][0] for info in infos}
    if not resolved_ips:
        raise SSRFBlockedError(f"No addresses resolved for host: {hostname}")

    for ip_str in resolved_ips:
        # IPv6 addresses from getaddrinfo can carry a zone index (e.g.
        # "fe80::1%eth0") that ipaddress.ip_address() rejects — strip it,
        # the zone doesn't change whether the address itself is blocked.
        ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        if _is_blocked_ip(ip):
            raise SSRFBlockedError(f"Blocked IP address for host {hostname}: {ip_str}")


@asynccontextmanager
async def safe_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_redirects: int = 5,
    **kwargs: Any,
):
    """Drop-in replacement for `session.get(url, allow_redirects=True)` that
    validates the destination — and every redirect hop — against SSRF
    targets before each request is made. `allow_redirects=True` alone only
    protects the first hop; a URL that looks public can still 302 to an
    internal address, which this closes by disabling aiohttp's automatic
    redirect handling and walking the chain manually.

    Usage: `async with safe_get(session, url) as response: ...` — same
    shape as `async with session.get(url) as response: ...`.
    """
    current_url = url
    hops = 0
    response: aiohttp.ClientResponse | None = None
    try:
        while True:
            await validate_url_for_fetch(current_url)
            response = await session.get(current_url, allow_redirects=False, **kwargs)

            is_redirect = response.status in (301, 302, 303, 307, 308)
            location = response.headers.get("Location") if is_redirect else None

            if is_redirect and location:
                if hops >= max_redirects:
                    response.release()
                    raise SSRFBlockedError(f"Too many redirects (> {max_redirects}) for {url}")
                next_url = urljoin(current_url, location)
                next_host = urlparse(next_url).hostname or "unknown"
                current_host = urlparse(current_url).hostname or "unknown"
                logger.info(
                    "Following redirect (re-validating target)",
                    extra={"from_host": current_host, "to_host": next_host, "hop": hops + 1},
                )
                response.release()
                current_url = next_url
                hops += 1
                continue

            yield response
            return
    finally:
        if response is not None:
            response.release()
