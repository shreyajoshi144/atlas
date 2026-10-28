"""Per-provider circuit breaker (closed -> open -> half_open)."""
from __future__ import annotations

import threading
import time
from typing import Callable


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.cooldown, self._clock = threshold, cooldown, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """True if a call may proceed. In half_open exactly one probe is admitted."""
        with self._lock:
            s = self._state_locked()
            if s == "closed":
                return True
            if s == "half_open" and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probe_in_flight = 0, None, False

    def failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._opened_at is not None or self._failures >= self.threshold:
                self._opened_at = self._clock()

    def release_probe(self) -> None:
        """A probe ended without counting for or against the provider (e.g. our own bad request)."""
        with self._lock:
            self._probe_in_flight = False
