"""Webhook translator registry — lookup by source name."""

from __future__ import annotations

from types import ModuleType
from typing import Any, Protocol


class WebhookTranslator(Protocol):
    """Protocol that translator modules must satisfy."""

    def translate(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def dedup_key(self, payload: dict[str, Any]) -> str | None: ...


# Lazy imports to keep the registry lightweight.
_TRANSLATORS: dict[str, str] = {
    "generic": "state_store.webhooks.generic",
    "horreum": "state_store.webhooks.horreum",
}


def _load_module(module_path: str) -> ModuleType:
    import importlib

    return importlib.import_module(module_path)


def get_translator(source: str) -> ModuleType:
    """Return the translator module for *source*, or raise KeyError."""
    module_path = _TRANSLATORS.get(source)
    if module_path is None:
        raise KeyError(f"Unknown webhook source: {source}")
    return _load_module(module_path)


def list_sources() -> list[str]:
    """Return sorted list of registered source names."""
    return sorted(_TRANSLATORS)
