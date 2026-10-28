"""ProviderClient: one call to one model with timeouts, retries, backoff, circuit breaking,
concurrency limits, metrics and latency measurement. Retries are never silent: every attempt is
recorded in ``attempts`` and surfaced to the caller/UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable

from .adapters import ADAPTERS
from .catalog import ModelEntry
from .circuit import CircuitBreaker
from .errors import BREAKER_TRIPPING, ProviderError
from .metrics import Metrics
from .settings import PROVIDERS, ProviderSettings
from .transport import HttpTransport, ResponseTooLarge, TransportError, TransportTimeout
from .models import NormalizedResponse, ProviderRequest

log = logging.getLogger("backend.providers.client")


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


class ProviderClient:
    def __init__(self, settings: ProviderSettings, transport: HttpTransport | None = None,
                 metrics: Metrics | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random) -> None:
        self.settings = settings
        self.transport = transport or HttpTransport(settings.allow_insecure_http, settings.max_response_bytes)
        self.metrics = metrics or Metrics()
        self._sleep, self._clock, self._rng = sleep, clock, rng
        self.breakers = {p: CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_seconds, clock) for p in PROVIDERS}
        self._global = asyncio.Semaphore(settings.global_concurrency)
        self._per = {p: asyncio.Semaphore(settings.per_provider_concurrency) for p in PROVIDERS}
        self.last_success: dict[str, float] = {}

    # ------------------------------------------------------------------
    def _delay(self, attempt_no: int, err: ProviderError) -> float:
        s = self.settings
        if err.retry_after is not None:
            return min(err.retry_after, s.retry_after_cap_seconds)
        return self._rng() * min(s.backoff_cap_seconds, s.backoff_base_seconds * (2 ** (attempt_no - 1)))

    async def _once(self, entry: ModelEntry, payload: tuple[str, dict[str, str], dict[str, Any]], timeout: float) -> NormalizedResponse:
        adapter = ADAPTERS[entry.provider]
        url, headers, body = payload
        try:
            resp = await self.transport.request("POST", url, headers, json.dumps(body).encode("utf-8"), timeout)
        except TransportTimeout:
            raise ProviderError("timeout", provider=entry.provider)
        except ResponseTooLarge:
            raise ProviderError("network", provider=entry.provider)
        except TransportError:
            raise ProviderError("network", provider=entry.provider)
        if 300 <= resp.status < 400:
            # Redirects are never followed (the key header must not travel to another host).
            raise ProviderError("network", http_status=resp.status, provider=entry.provider)
        if resp.status >= 400:
            raise adapter.error_from(resp.status, resp.headers, resp.body)
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("parse", provider=entry.provider)
        return adapter.parse(entry, data)

    async def generate(self, entry: ModelEntry, req: ProviderRequest) -> NormalizedResponse:
        s = self.settings
        provider = entry.provider
        if not s.configured(provider):
            raise ProviderError("config", provider=provider)
        breaker = self.breakers[provider]
        if not breaker.allow():
            self._record(entry, "circuit_open", "circuit_open", None, [])
            raise ProviderError("circuit_open", provider=provider)
        adapter = ADAPTERS[provider]
        payload = adapter.build(entry, req, s)
        attempts: list[dict[str, Any]] = []
        started = self._clock()
        deadline = started + s.deadline_seconds
        probe_done = False
        try:
            async with self._global, self._per[provider]:
                for n in range(1, s.max_retries + 2):
                    remaining = deadline - self._clock()
                    if remaining <= 0.05:
                        raise ProviderError("timeout", provider=provider, attempts=attempts)
                    t0 = self._clock()
                    try:
                        result = await self._once(entry, payload, min(s.timeout_seconds, remaining))
                    except ProviderError as err:
                        ms = int((self._clock() - t0) * 1000)
                        err.attempts = attempts
                        attempts.append({"n": n, "ms": ms, "outcome": "timeout" if err.category == "timeout" else "error",
                                         "category": err.category})
                        if err.category in BREAKER_TRIPPING:
                            breaker.failure()
                            probe_done = True
                        if not err.retryable or n > s.max_retries:
                            raise
                        delay = self._delay(n, err)
                        if self._clock() + delay >= deadline - 0.05:
                            raise
                        log.info("provider retry", extra={"provider": provider, "model": entry.model,
                                                          "attempt": n, "category": err.category,
                                                          "delay_s": round(delay, 2), "request_id": req.request_id})
                        await self._sleep(delay)
                        if not breaker.allow() and breaker.state == "open":
                            raise ProviderError("circuit_open", provider=provider, attempts=attempts)
                        continue
                    ms = int((self._clock() - t0) * 1000)
                    attempts.append({"n": n, "ms": ms, "outcome": "ok", "category": None})
                    breaker.success()
                    probe_done = True
                    self.last_success[provider] = time.time()
                    result.attempts = attempts
                    result.total_ms = int((self._clock() - started) * 1000)
                    self._record(entry, "ok", None, result, attempts)
                    return result
                raise ProviderError("internal", provider=provider, attempts=attempts)   # pragma: no cover
        except ProviderError as err:
            err.attempts = attempts
            if not probe_done:
                breaker.release_probe()
            if err.category != "circuit_open":
                self._record(entry, "error", err.category, None, attempts, int((self._clock() - started) * 1000))
            log.warning("provider call failed", extra={"provider": provider, "model": entry.model, "category": err.category,
                                                       "attempts": len(attempts), "request_id": req.request_id})
            raise
        except asyncio.CancelledError:
            if not probe_done:
                breaker.release_probe()
            raise

    def _record(self, entry: ModelEntry, outcome: str, category: str | None, result: NormalizedResponse | None,
                attempts: list[dict[str, Any]], total_ms: int | None = None) -> None:
        ms = result.total_ms if result else total_ms
        self.metrics.record(entry.provider, entry.model, outcome, category, ms, max(0, len(attempts) - 1),
                            result.usage.input_tokens if result else None, result.usage.output_tokens if result else None, None)

    def circuit_state(self, provider: str) -> str:
        return self.breakers[provider].state

    def circuit_states(self) -> dict[str, str]:
        return {p: b.state for p, b in self.breakers.items()}

    async def list_models(self, provider: str) -> set[str]:
        """Model IDs this key can use (free call). Follows pagination; raises ProviderError."""
        s = self.settings
        if not s.configured(provider):
            raise ProviderError("config", provider=provider)
        adapter = ADAPTERS[provider]
        ids: set[str] = set()
        token: str | None = None
        for _ in range(20):
            url, headers = adapter.list_models_request(s, token)
            try:
                resp = await self.transport.request("GET", url, headers, None, min(s.timeout_seconds, 30.0))
            except TransportTimeout:
                raise ProviderError("timeout", provider=provider)
            except (TransportError, ResponseTooLarge):
                raise ProviderError("network", provider=provider)
            if resp.status >= 300:
                raise adapter.error_from(resp.status, resp.headers, resp.body)
            try:
                page, token = adapter.parse_models(json.loads(resp.body.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                raise ProviderError("parse", provider=provider)
            ids |= page
            if not token:
                return ids
        return ids

    def health(self) -> list[dict[str, Any]]:
        return [{"provider": p, "configured": self.settings.configured(p), "circuit": self.breakers[p].state,
                 "last_success_epoch": self.last_success.get(p)} for p in PROVIDERS]
