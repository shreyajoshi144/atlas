"""Minimal HTTPS transport on the standard library.

Design choices (all deliberate):
* ``http.client`` instead of urllib: redirects are NEVER followed, so an API key
  header can never be replayed to a different host by a redirect.
* A hard wall-clock deadline is checked while reading, so a trickling server
  cannot hold a worker thread forever; the response size is capped.
* Runs in a dedicated thread pool so provider calls cannot starve the app's
  default executor.
* Honours HTTPS_PROXY / NO_PROXY via a CONNECT tunnel.
"""
from __future__ import annotations

import asyncio
import http.client
import os
import socket
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="atlas-provider")


class TransportTimeout(Exception):
    pass


class TransportError(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # declared dependency of Atlas
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _blocking(method: str, url: str, headers: dict[str, str], body: bytes | None,
              timeout: float, max_bytes: int, allow_http: bool) -> HttpResponse:
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise TransportError("bad url")
    if u.scheme == "http" and not allow_http:
        raise TransportError("insecure http not allowed")
    deadline = time.monotonic() + timeout
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn: http.client.HTTPConnection
    try:
        proxy = urllib.request.getproxies().get(u.scheme)
        use_proxy = bool(proxy) and not urllib.request.proxy_bypass(u.hostname)
        if u.scheme == "https":
            ctx = _ssl_context()
            if use_proxy:
                p = urlparse(proxy)
                conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=timeout, context=ctx)
                conn.set_tunnel(u.hostname, port)
            else:
                conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
        try:
            hdrs = dict(headers)
            hdrs.setdefault("Accept", "application/json")
            hdrs.setdefault("User-Agent", "AtlasAI-Compare/1.0")
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise TransportTimeout()
                conn.sock.settimeout(max(0.05, deadline - time.monotonic())) if conn.sock else None
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge()
                chunks.append(chunk)
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, b"".join(chunks))
        finally:
            conn.close()
    except (socket.timeout, TimeoutError, TransportTimeout) as exc:
        raise TransportTimeout() from exc
    except ResponseTooLarge:
        raise
    except (ssl.SSLError, ConnectionError, socket.gaierror, http.client.HTTPException, OSError) as exc:
        raise TransportError(type(exc).__name__) from exc


class HttpTransport:
    def __init__(self, allow_insecure_http: bool = False, max_response_bytes: int = 4 * 1024 * 1024) -> None:
        self.allow_insecure_http = allow_insecure_http
        self.max_response_bytes = max_response_bytes

    async def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                      timeout: float) -> HttpResponse:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_EXECUTOR, _blocking, method, url, headers, body, timeout,
                                   self.max_response_bytes, self.allow_insecure_http)
        try:
            return await asyncio.wait_for(fut, timeout + 1.0)
        except asyncio.TimeoutError as exc:
            raise TransportTimeout() from exc
