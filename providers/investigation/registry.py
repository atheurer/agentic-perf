"""Registry for Investigation Record storage backends.

Lazy-loads the configured backend from ~/.agentic-perf/config.json.
Defaults to the file-based provider if no backend is configured.

Configuration example in config.json:
    {
        "investigation_records": {
            "backend": "file",
            "persist_dir": "/path/to/records"
        }
    }

Future backends (horreum, opensearch, etc.) register in
BACKEND_REGISTRY with their module path and class name.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

from .base import InvestigationRecordProvider

logger = logging.getLogger(__name__)

# Backend registry — maps backend names to their implementation.
# Each entry specifies the module path and class name. New backends
# add an entry here; no other code changes needed.
BACKEND_REGISTRY: dict[str, dict[str, str]] = {
    "file": {
        "class": ("providers.investigation.file.FileRecordProvider"),
    },
    # Future backends:
    # "horreum": {
    #     "class": (
    #         "providers.investigation.horreum"
    #         ".HorreumRecordProvider"
    #     ),
    # },
    # "opensearch": {
    #     "class": (
    #         "providers.investigation.opensearch"
    #         ".OpenSearchRecordProvider"
    #     ),
    # },
}

_CONFIG_PATH = Path.home() / ".agentic-perf" / "config.json"


def _load_config() -> dict[str, Any]:
    """Load investigation_records config from config.json."""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg.get("investigation_records", {})
    except (json.JSONDecodeError, OSError):
        return {}


def create_record_provider(
    backend: str | None = None,
    **kwargs: Any,
) -> InvestigationRecordProvider:
    """Create a record provider from config or explicit args.

    Args:
        backend: Backend name (file, horreum, opensearch, etc.).
            If None, reads from config.json. Defaults to "file".
        **kwargs: Passed to the backend constructor (e.g.,
            persist_dir for file, url for horreum).

    Returns:
        A configured InvestigationRecordProvider instance.
    """
    config = _load_config()
    backend_name = backend or config.get("backend", "file")

    entry = BACKEND_REGISTRY.get(backend_name)
    if entry is None:
        available = list(BACKEND_REGISTRY.keys())
        raise ValueError(
            f"Unknown investigation record backend: "
            f"{backend_name!r}. "
            f"Available: {available}"
        )

    # Merge config values with explicit kwargs
    # (explicit kwargs take precedence)
    merged = {**config, **kwargs}
    merged.pop("backend", None)

    module_path, cls_name = entry["class"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, cls_name)

    provider = cls(**merged)
    logger.info(f"[investigation] Using {backend_name} backend ({cls_name})")
    return provider
