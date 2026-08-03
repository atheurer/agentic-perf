"""Cascading secrets provider for per-user secret resolution.

Layers a sequence of SecretsProvider instances (e.g. user-private,
group-shared, deployment-shared) and returns the first hit.  Shadow
detection logs when an earlier layer masks a later one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from .base import SecretsProvider
from .local import LocalSecretsProvider

logger = logging.getLogger(__name__)


class CascadingSecretsProvider(SecretsProvider):
    """Resolve secrets through ordered layers; first hit wins.

    Each layer is a ``(label, SecretsProvider)`` pair.  Labels are
    used only for logging (e.g. ``"user:alice"``, ``"group:gpu-team"``,
    ``"shared"``).
    """

    def __init__(self, layers: list[tuple[str, SecretsProvider]]) -> None:
        if not layers:
            raise ValueError("CascadingSecretsProvider requires at least one layer")
        self._layers = layers

    async def get_secret(self, path: str) -> str | None:
        winner_label: str | None = None
        winner_value: str | None = None

        for label, provider in self._layers:
            value = await provider.get_secret(path)
            if value is not None:
                if winner_value is None:
                    winner_label = label
                    winner_value = value
                else:
                    logger.info(
                        "Secret '%s' in layer '%s' shadowed by '%s'",
                        path,
                        label,
                        winner_label,
                    )
        return winner_value

    async def get_secret_file(self, path: str) -> Path | None:
        winner_label: str | None = None
        winner_path: Path | None = None

        for label, provider in self._layers:
            file_path = await provider.get_secret_file(path)
            if file_path is not None:
                if winner_path is None:
                    winner_label = label
                    winner_path = file_path
                else:
                    logger.info(
                        "Secret file '%s' in layer '%s' shadowed by '%s'",
                        path,
                        label,
                        winner_label,
                    )
        return winner_path

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        """Yield a file path for the winning layer's secret.

        Probes layers via ``get_secret()`` first, then falls back to
        ``get_secret_file()`` so that both vault providers (text-only)
        and local providers with binary files can participate in
        cascade selection.
        """
        winner_label: str | None = None
        winner_provider: SecretsProvider | None = None

        for label, provider in self._layers:
            has = await provider.get_secret(path) is not None
            if not has:
                has = await provider.get_secret_file(path) is not None
            if has:
                if winner_provider is None:
                    winner_label = label
                    winner_provider = provider
                else:
                    logger.info(
                        "Secret file '%s' in layer '%s' shadowed by '%s'",
                        path,
                        label,
                        winner_label,
                    )

        if winner_provider is None:
            yield None
        else:
            async with winner_provider.secret_file(path) as p:
                yield p

    async def list_secrets(self, prefix: str = "") -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for _, provider in self._layers:
            for secret in await provider.list_secrets(prefix):
                if secret not in seen:
                    seen.add(secret)
                    result.append(secret)
        return result


def build_cascade_for_user(
    username: str,
    groups: list[str],
    secrets_root: Path,
    vault_config: dict | None = None,
) -> CascadingSecretsProvider:
    """Build a per-user secrets cascade.

    Layer order (first wins):
    1. ``secrets_root/users/<username>/`` — user-private secrets
    2. For each group (alphabetical):
       a. ``secrets_root/groups/<group>/`` — local group secrets
       b. ``vault:group:<group>`` — vault group secrets (if configured)
    3. ``secrets_root/`` — shared deployment secrets (local)
    4. ``vault:shared`` — shared deployment secrets (vault, if configured)

    Within a tier, local directory beats vault — a file on disk is an
    explicit operator override.  Vault fills in behind local.

    Layers whose directories don't exist are silently skipped.
    Vault layers are only added when ``vault_config`` contains a
    ``bitwarden`` section with the relevant project ID.
    """
    layers: list[tuple[str, SecretsProvider]] = []
    bw_config = (vault_config or {}).get("bitwarden", {})

    user_dir = secrets_root / "users" / username
    if user_dir.is_dir():
        layers.append((f"user:{username}", LocalSecretsProvider(user_dir)))

    for group in sorted(groups):
        group_dir = secrets_root / "groups" / group
        if group_dir.is_dir():
            layers.append(
                (f"group:{group}", LocalSecretsProvider(group_dir)),
            )
        group_project_id = bw_config.get("group_project_ids", {}).get(group)
        if group_project_id:
            vault_provider = _create_vault_layer(bw_config, group_project_id)
            if vault_provider is not None:
                layers.append((f"vault:group:{group}", vault_provider))

    layers.append(
        (
            "shared",
            LocalSecretsProvider(
                secrets_root,
                exclude_prefixes=["users", "groups"],
            ),
        )
    )

    shared_project_id = bw_config.get("shared_project_id")
    if shared_project_id:
        vault_provider = _create_vault_layer(bw_config, shared_project_id)
        if vault_provider is not None:
            layers.append(("vault:shared", vault_provider))

    return CascadingSecretsProvider(layers)


def _create_vault_layer(
    bw_config: dict,
    project_id: str,
) -> SecretsProvider | None:
    """Try to construct a Bitwarden vault layer.

    Returns ``None`` (with a warning) only if the optional SDK is not
    installed.  All other errors (missing config, bad token, auth
    failure) propagate — a configured vault that cannot authenticate
    is a deployment error, not a soft dependency.
    """
    try:
        from .factory import create_bitwarden_provider

        return create_bitwarden_provider(
            organization_id=bw_config["organization_id"],
            project_id=project_id,
            server_url=bw_config.get("server_url"),
            cache_ttl_seconds=bw_config.get("cache_ttl_seconds", 60),
        )
    except ImportError:
        logger.warning(
            "bitwarden-sdk not installed; skipping vault layer for project %s",
            project_id,
        )
        return None
