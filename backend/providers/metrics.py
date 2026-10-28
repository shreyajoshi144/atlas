"""In-process metrics (single process). Exposed to admins as JSON or Prometheus text."""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

_BUCKETS_MS = (250, 500, 1000, 2000, 5000, 10000, 20000, 40000, 80000, 160000)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: dict[tuple[str, str, str], int] = defaultdict(int)      # (provider, model, outcome)
        self.errors: dict[tuple[str, str], int] = defaultdict(int)          # (provider, category)
        self.retries: dict[str, int] = defaultdict(int)
        self.tokens: dict[tuple[str, str], int] = defaultdict(int)          # (provider, in|out)
        self.cost_usd: dict[str, float] = defaultdict(float)
        self.latency_sum: dict[str, float] = defaultdict(float)
        self.latency_count: dict[str, int] = defaultdict(int)
        self.latency_buckets: dict[tuple[str, int], int] = defaultdict(int)

    def record(self, provider: str, model: str, outcome: str, category: str | None, ms: int | None,
               retries: int, tokens_in: int | None, tokens_out: int | None, cost: float | None) -> None:
        with self._lock:
            self.calls[(provider, model, outcome)] += 1
            if category:
                self.errors[(provider, category)] += 1
            self.retries[provider] += retries
            if tokens_in:
                self.tokens[(provider, "in")] += tokens_in
            if tokens_out:
                self.tokens[(provider, "out")] += tokens_out
            if cost:
                self.cost_usd[provider] += cost
            if ms is not None:
                self.latency_sum[provider] += ms
                self.latency_count[provider] += 1
                for b in _BUCKETS_MS:
                    if ms <= b:
                        self.latency_buckets[(provider, b)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            models: dict[str, dict[str, int]] = {}
            for (p, mdl, o), n in self.calls.items():
                models.setdefault(f"{p}:{mdl}", {})[o] = n
            return {
                "models": models,
                "calls": [{"provider": p, "model": m, "outcome": o, "count": n} for (p, m, o), n in sorted(self.calls.items())],
                "errors": [{"provider": p, "category": c, "count": n} for (p, c), n in sorted(self.errors.items())],
                "retries": dict(self.retries),
                "tokens": [{"provider": p, "kind": k, "count": n} for (p, k), n in sorted(self.tokens.items())],
                "estimated_cost_usd": {p: round(v, 6) for p, v in self.cost_usd.items()},
                "latency_ms_avg": {p: round(self.latency_sum[p] / self.latency_count[p], 1)
                                   for p in self.latency_count if self.latency_count[p]},
            }

    def prometheus(self, circuit_states: dict[str, str] | None = None) -> str:
        snap = self.snapshot()
        out = ["# TYPE atlas_provider_calls_total counter"]
        for r in snap["calls"]:
            out.append(f'atlas_provider_calls_total{{provider="{r["provider"]}",model="{r["model"]}",outcome="{r["outcome"]}"}} {r["count"]}')
        out.append("# TYPE atlas_provider_errors_total counter")
        for r in snap["errors"]:
            out.append(f'atlas_provider_errors_total{{provider="{r["provider"]}",category="{r["category"]}"}} {r["count"]}')
        out.append("# TYPE atlas_provider_retries_total counter")
        for p, n in snap["retries"].items():
            out.append(f'atlas_provider_retries_total{{provider="{p}"}} {n}')
        out.append("# TYPE atlas_provider_latency_ms histogram")
        with self._lock:
            for p in self.latency_count:
                for b in _BUCKETS_MS:
                    out.append(f'atlas_provider_latency_ms_bucket{{provider="{p}",le="{b}"}} {self.latency_buckets[(p, b)]}')
                out.append(f'atlas_provider_latency_ms_bucket{{provider="{p}",le="+Inf"}} {self.latency_count[p]}')
                out.append(f'atlas_provider_latency_ms_sum{{provider="{p}"}} {self.latency_sum[p]}')
                out.append(f'atlas_provider_latency_ms_count{{provider="{p}"}} {self.latency_count[p]}')
        if circuit_states:
            out.append("# TYPE atlas_provider_circuit_open gauge")
            for p, st in circuit_states.items():
                out.append(f'atlas_provider_circuit_open{{provider="{p}"}} {0 if st == "closed" else 1}')
        return "\n".join(out) + "\n"
