"""Model catalog: which models Atlas offers, how they are priced, and how fresh that knowledge is.

The catalog is data (catalog.json), not code. Operators can point
``MODEL_CATALOG_PATH`` at their own file. Nothing here is a claim about quality.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .settings import PROVIDERS

STALE_AFTER_DAYS = 60
_DEFAULT_PATH = Path(__file__).with_name("catalog.json")
_STRUCTURED_NATIVE = {"openai": True, "xai": True, "gemini": False, "anthropic": False}


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model: str
    label: str
    tier: str
    pricing: tuple[dict[str, Any], ...]
    default: bool = False
    price_note: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    tool_prices: dict[str, Any] = field(default_factory=dict)
    source_url: str = ""
    verified_on: str = ""

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def structured_native(self) -> bool:
        return _STRUCTURED_NATIVE.get(self.provider, False)

    @property
    def native_search_supported(self) -> bool:
        return True


class CatalogError(ValueError):
    pass


class Catalog:
    """Built from the parsed catalog.json dict (validated)."""

    def __init__(self, data: dict[str, Any]) -> None:
        verified = data.get("verified_on", "")
        entries: list[ModelEntry] = []
        for raw in data["models"]:
            if raw["provider"] not in PROVIDERS:
                raise CatalogError(f"unknown provider {raw['provider']!r}")
            _validate_pricing(raw["model"], raw["pricing"])
            entries.append(ModelEntry(
                provider=raw["provider"], model=raw["model"], label=raw.get("label", raw["model"]),
                tier=raw.get("tier", "balanced"), pricing=tuple(raw["pricing"]), default=bool(raw.get("default")),
                price_note=raw.get("price_note"), context_window=raw.get("context_window"),
                max_output_tokens=raw.get("max_output_tokens"), tool_prices=dict(raw.get("tool_prices") or {}),
                source_url=raw.get("source_url", ""), verified_on=raw.get("verified_on", verified)))
        ids = [e.id for e in entries]
        if len(ids) != len(set(ids)):
            raise CatalogError("duplicate model ids in catalog")
        self.entries = {e.id: e for e in entries}
        self.verified_on = verified
        self.version = str(data.get("catalog_version", ""))
        self.notes = data.get("notes", "")

    def get(self, model_id: str) -> ModelEntry | None:
        return self.entries.get(model_id)

    def all(self) -> list[ModelEntry]:
        return list(self.entries.values())

    def for_provider(self, provider: str) -> list[ModelEntry]:
        return [e for e in self.entries.values() if e.provider == provider]

    by_provider = for_provider

    def is_stale(self, today: date | None = None) -> bool:
        today = today or date.today()
        try:
            return (today - date.fromisoformat(self.verified_on)).days > STALE_AFTER_DAYS
        except ValueError:
            return True


def _validate_pricing(model: str, pricing: list[dict[str, Any]]) -> None:
    if not pricing:
        raise CatalogError(f"{model}: pricing missing")
    for sched in pricing:
        date.fromisoformat(sched["effective_from"])
        tiers = sched["tiers"]
        if not tiers or tiers[-1].get("max_prompt_tokens") is not None:
            raise CatalogError(f"{model}: last price tier must be open-ended (max_prompt_tokens null)")
        for t in tiers:
            if not (isinstance(t["in"], (int, float)) and isinstance(t["out"], (int, float)) and t["in"] >= 0 and t["out"] >= 0):
                raise CatalogError(f"{model}: invalid price")


def load_catalog(path: str | Path | None = None) -> Catalog:
    p = Path(path or os.environ.get("MODEL_CATALOG_PATH") or _DEFAULT_PATH)
    return Catalog(json.loads(p.read_text(encoding="utf-8")))
