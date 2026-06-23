from __future__ import annotations

from .base import SecretsProvider


def create_secrets_provider(
    backend: str = "local", **config
) -> SecretsProvider:
    """Create a SecretsProvider from a backend name and config.

    Supported backends:
      - "local": file-backed secrets in a directory (default ~/.agentic-perf/secrets/)
        Config: path (optional) — override the secrets directory

    To add a new vault backend: create a SecretsProvider subclass, add an
    elif branch here. No agent or MCP server changes needed.
    """
    if backend == "local":
        from .local import LocalSecretsProvider

        return LocalSecretsProvider(config.get("path"))

    raise ValueError(
        f"Unknown secrets backend: {backend!r}. Supported: 'local'"
    )
