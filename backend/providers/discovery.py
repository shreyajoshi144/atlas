"""Live model discovery: ask each provider which model IDs this key can use.

Listing models is free (no tokens). Results are cached; a model is marked
``available=False`` only when a *complete* listing succeeded and omitted it.
"""
from __future__ import annotations

import time
from typing import Any

from .catalog import Catalog
from .client import ProviderClient
from .errors import ProviderError
from .settings import PROVIDERS


class DiscoveryCache:
    def __init__(self, client: ProviderClient, catalog: Catalog, settings: Any | None = None) -> None:
        self.client, self.catalog = client, catalog
        self.ttl = (settings or client.settings).discovery_ttl_seconds
        self._cache: dict[str, tuple[float, set[str]]] = {}
        self.last_report: dict[str, Any] = {}

    def availability(self, entry: Any) -> bool | None:
        """True/False only when a complete, recent listing exists; otherwise None (unknown)."""
        hit = self._cache.get(entry.provider)
        if not hit or time.time() - hit[0] > max(self.ttl, 1.0) * 2:
            return None
        return entry.model in hit[1]

    async def refresh(self, providers: list[str] | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for provider in (providers or PROVIDERS):
            if not self.client.settings.configured(provider):
                report[provider] = {"status": "not_configured"}
                continue
            try:
                ids = await self.client.list_models(provider)
            except ProviderError as exc:
                report[provider] = {"status": "error", "error": exc.public()}
                continue
            self._cache[provider] = (time.time(), ids)
            cat = {e.model for e in self.catalog.for_provider(provider)}
            report[provider] = {"status": "ok", "listed": len(ids),
                                "catalog_models_missing": sorted(cat - ids),
                                "catalog_models_present": sorted(cat & ids)}
        self.last_report = report
        return report
