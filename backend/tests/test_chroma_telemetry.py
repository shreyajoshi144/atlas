"""Chroma product telemetry must never run (posthog>=6 broke chromadb's capture call)."""
from __future__ import annotations

import logging
from types import SimpleNamespace


def test_chroma_client_never_calls_posthog_capture(tmp_path, monkeypatch, caplog):
    import posthog
    from chromadb.telemetry.product import ProductTelemetryClient
    import backend.memory.chroma_client as mod

    calls = []
    monkeypatch.setattr(posthog, "capture", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(chroma_path=str(tmp_path / "chroma")))
    monkeypatch.setattr(mod, "_get_embedding_function", lambda: None)

    with caplog.at_level(logging.ERROR, logger="chromadb"):
        client = mod.ChromaMemoryClient()
        client.get_collection()          # ClientStartEvent + ClientCreateCollectionEvent previously fired here

    assert calls == []
    assert isinstance(client.client._system.instance(ProductTelemetryClient), mod.NoOpProductTelemetry)
    assert not [r for r in caplog.records if "telemetry" in r.getMessage().lower()]
