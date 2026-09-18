"""Tests for EventBus resource ownership and deterministic shutdown."""

from __future__ import annotations

from pathlib import Path

from providers.events import EventBus
from state_store.trace_store import TraceStore


def test_close_releases_owned_trace_store(tmp_path: Path) -> None:
    bus = EventBus(log_dir=tmp_path / "logs")
    store = bus._trace_store

    assert store._connection is not None
    bus.close()

    assert store._connection is None
    bus.close()


def test_close_preserves_injected_trace_store_ownership(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        bus = EventBus(log_dir=tmp_path / "logs", trace_store=store)

        bus.close()

        assert store._connection is not None
