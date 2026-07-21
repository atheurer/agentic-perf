"""Cascading secrets provider for per-user secret resolution.

Layers a sequence of SecretsProvider instances (e.g. user-private,
group-shared, deployment-shared) and returns the first hit.  Shadow
detection logs when an earlier layer masks a later one.
"""

from __future__ import annotations

import logging
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
) -> CascadingSecretsProvider:
    """Build a per-user secrets cascade.

    Layer order (first wins):
    1. ``secrets_root/users/<username>/`` — user-private secrets
    2. ``secrets_root/groups/<group>/`` for each group (alphabetical)
    3. ``secrets_root/`` — shared deployment secrets

    Layers whose directories don't exist are silently skipped.
    """
    layers: list[tuple[str, SecretsProvider]] = []

    user_dir = secrets_root / "users" / username
    if user_dir.is_dir():
        layers.append((f"user:{username}", LocalSecretsProvider(user_dir)))

    for group in sorted(groups):
        group_dir = secrets_root / "groups" / group
        if group_dir.is_dir():
            layers.append((f"group:{group}", LocalSecretsProvider(group_dir)))

    layers.append(
        (
            "shared",
            LocalSecretsProvider(
                secrets_root,
                exclude_prefixes=["users", "groups"],
            ),
        )
    )

    return CascadingSecretsProvider(layers)
