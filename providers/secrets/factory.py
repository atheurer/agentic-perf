"""Factory functions for constructing secrets providers.

Centralizes provider creation so that the orchestrator, dispatcher, and
agent MCP servers share one construction path.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import SecretsProvider

logger = logging.getLogger(__name__)


def create_secrets_provider(
    backend: str = "local",
    **config: Any,
) -> SecretsProvider:
    """Create a SecretsProvider from a backend name and config.

    Supported backends:
      - "local": file-backed secrets in a directory
        (default ``~/.agentic-perf/secrets/``).
        Config: ``path`` (optional) — override the secrets directory.

    Vault backends (Bitwarden Secrets Manager) are always used as
    overlay layers in a ``CascadingSecretsProvider``, not as
    standalone backends.  Use ``create_bitwarden_provider()`` via
    the cascade wiring in ``build_cascade_for_user()`` or
    ``build_secrets_provider()``.
    """
    if backend == "local":
        from .local import LocalSecretsProvider

        return LocalSecretsProvider(config.get("path"))

    raise ValueError(f"Unknown secrets backend: {backend!r}. Supported: 'local'")


def create_bitwarden_provider(
    organization_id: str,
    project_id: str,
    server_url: str | None = None,
    cache_ttl_seconds: float = 60,
) -> SecretsProvider:
    """Create a ``BitwardenSecretsProvider`` with token auto-resolution.

    The access token is resolved from env ``AGENTIC_PERF_BWS_TOKEN`` or
    file ``~/.agentic-perf/secrets/bitwarden/access-token``.  Raises
    ``SecretsBackendError`` if neither is available.

    This is the single construction helper called by both the
    orchestrator (for cascade vault layers) and agent MCP servers
    (for standalone vault providers).
    """
    from .bitwarden import BitwardenSecretsProvider, resolve_access_token

    return BitwardenSecretsProvider(
        organization_id=organization_id,
        project_id=project_id,
        access_token=resolve_access_token(),
        server_url=server_url,
        cache_ttl_seconds=cache_ttl_seconds,
    )
