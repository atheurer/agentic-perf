"""Registry for Investigation Record storage backends.

Lazy-loads the configured backend from ~/.agentic-perf/config.json.
Defaults to the file-based provider if no backend is configured.

Single-backend configuration:
    {
        "investigation_records": {
            "backend": "file",
            "persist_dir": "/path/to/records"
        }
    }

Multi-read (composite) configuration — one writer, multiple readers:
    {
        "investigation_records": {
            "backend": "composite",
            "writer": {"backend": "opensearch", "url": "..."},
            "readers": [
                {"backend": "opensearch", "url": "..."},
                {"backend": "file", "persist_dir": "/old/records"}
            ]
        }
    }

New backends register in BACKEND_REGISTRY with their module path
and class name.
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
    # "opensearch": {
    #     "class": (
    #         "providers.investigation.opensearch"
    #         ".OpenSearchRecordProvider"
    #     ),
    # },
    # "horreum": {
    #     "class": (
    #         "providers.investigation.horreum"
    #         ".HorreumRecordProvider"
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


def _create_single_provider(
    config: dict[str, Any],
) -> InvestigationRecordProvider:
    """Create a single backend provider from a config dict.

    Args:
        config: Must contain "backend" key. Other keys are
            passed to the backend constructor.
    """
    backend_name = config.get("backend", "file")

    entry = BACKEND_REGISTRY.get(backend_name)
    if entry is None:
        available = list(BACKEND_REGISTRY.keys())
        raise ValueError(
            f"Unknown investigation record backend: "
            f"{backend_name!r}. "
            f"Available: {available}"
        )

    # Pass all config except "backend" to the constructor
    kwargs = {k: v for k, v in config.items() if k != "backend"}

    module_path, cls_name = entry["class"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, cls_name)

    provider = cls(**kwargs)
    logger.info(f"[investigation] Created {backend_name} backend ({cls_name})")
    return provider


def create_record_provider(
    backend: str | None = None,
    **kwargs: Any,
) -> InvestigationRecordProvider:
    """Create a record provider from config or explicit args.

    Supports two modes:

    Single backend (default):
        create_record_provider(backend="file", persist_dir="...")

    Composite (one writer, multiple readers):
        Configured via config.json with backend="composite",
        a "writer" dict, and a "readers" list.

    Args:
        backend: Backend name. If None, reads from config.json.
            Defaults to "file". Use "composite" for multi-read.
        **kwargs: Passed to the backend constructor for single-
            backend mode. Ignored for composite mode (reads
            writer/readers from config).

    Returns:
        A configured InvestigationRecordProvider instance.
    """
    config = _load_config()
    backend_name = backend or config.get("backend", "file")

    if backend_name == "composite":
        return _create_composite(config)

    # Single backend — merge config with explicit kwargs
    merged = {**config, **kwargs}
    merged["backend"] = backend_name
    return _create_single_provider(merged)


def _create_composite(
    config: dict[str, Any],
) -> InvestigationRecordProvider:
    """Create a composite provider from config.

    Expects config to have:
        "writer": {"backend": "...", ...}
        "readers": [{"backend": "...", ...}, ...]
    """
    from .composite import CompositeRecordProvider

    writer_config = config.get("writer")
    if not writer_config:
        raise ValueError("Composite backend requires a 'writer' config")

    readers_config = config.get("readers", [])
    if not readers_config:
        raise ValueError("Composite backend requires a 'readers' list")

    writer = _create_single_provider(writer_config)

    readers = []
    for reader_config in readers_config:
        readers.append(_create_single_provider(reader_config))

    # If the writer isn't in the readers list, prepend it
    # so its authoritative copies take precedence in queries
    writer_in_readers = any(r is writer for r in readers)
    if not writer_in_readers:
        readers.insert(0, writer)

    logger.info(
        f"[investigation] Composite backend: "
        f"writer={writer.provider_name}, "
        f"{len(readers)} readers"
    )
    return CompositeRecordProvider(writer=writer, readers=readers)
