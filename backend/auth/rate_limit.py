from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status


class _InMemoryRateLimiter:
    """Fixed-window request counter keyed by (bucket, client IP).

    Deliberately minimal — no Redis/external store, matching the existing
    single-process architecture. This means limits are per-process: fine
    for the current single-instance deployment, but won't be shared across
    multiple app instances/workers behind a load balancer (see the
    known-risks note in the security report). Good enough to blunt casual
    credential-stuffing / registration-spam / reset-token-spam against a
    single deployed instance without adding a new dependency.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many attempts. Please wait a moment and try again.",
                )
            bucket.append(now)

    def reset(self) -> None:
        """Test-only: clear all counters between test cases."""
        with self._lock:
            self._hits.clear()


_limiter = _InMemoryRateLimiter()


def rate_limit(bucket_name: str, limit: int, window_seconds: int):
    """FastAPI dependency factory. Usage:

        @router.post("/login", dependencies=[Depends(rate_limit("login", 10, 60))])

    Applied only to the specific auth-sensitive routes that need it — not
    registered as global middleware, per the "focused, not broad" scope.
    """

    async def _dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        _limiter.check(f"{bucket_name}:{client_ip}", limit=limit, window_seconds=window_seconds)

    return _dependency


def reset_rate_limits() -> None:
    """Test helper — avoids one test's requests counting against another's
    limit when they share the same in-process limiter instance."""
    _limiter.reset()


class _InMemoryConcurrencyGuard:
    """Single-process guard preventing concurrent expensive runs per user.

    This is intentionally local to the current deployment model; adding a
    distributed lock would require new infrastructure, which is outside M4.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A research run is already in progress for this account.",
                )
            self._active.add(user_id)

    def release(self, user_id: str) -> None:
        with self._lock:
            self._active.discard(user_id)

    def reset(self) -> None:
        with self._lock:
            self._active.clear()


_run_guard = _InMemoryConcurrencyGuard()

def acquire_research_run(user_id: str) -> None:
    _run_guard.acquire(user_id)

def release_research_run(user_id: str) -> None:
    _run_guard.release(user_id)

def reset_research_run_guards() -> None:
    _run_guard.reset()


def rate_limit_by_user(bucket_name: str, limit: int, window_seconds: int):
    """Same idea as rate_limit(), but keyed by authenticated user_id rather
    than client IP — appropriate for the expensive, already-authenticated
    endpoints (research generation, chat, semantic search) where multiple
    legitimate users can share an IP (NAT, campus network, etc.) and an
    IP-keyed limit would wrongly throttle all of them together.

    Depends(get_current_user) here reuses the same cached result FastAPI
    already computed for the route's own auth dependency within one
    request — this doesn't re-run auth or add a second token check.
    """
    from backend.auth.dependencies import get_current_user

    async def _dependency(current_user: dict = Depends(get_current_user)) -> None:
        _limiter.check(f"{bucket_name}:{current_user['user_id']}", limit=limit, window_seconds=window_seconds)

    return _dependency
