from __future__ import annotations

import logging
import os
from pathlib import Path

from paths import SECRETS_DIR as DEFAULT_SECRETS_DIR

from .base import SecretsProvider

logger = logging.getLogger(__name__)


class LocalSecretsProvider(SecretsProvider):
    """File-backed secrets provider for prototype use.

    Reads secrets from a local directory. Each secret is a file; the path
    argument maps directly to the filesystem path relative to the secrets dir.

    Example:
        get_secret("crucible/crucible-client-server-token.json")
        → reads ~/.agentic-perf/secrets/crucible/crucible-client-server-token.json

    Admin setup: symlink or copy credential files into the secrets directory.
    When HashiCorp Vault is available, swap this provider for VaultSecretsProvider
    with the same interface.
    """

    def __init__(self, secrets_dir: str | Path | None = None) -> None:
        self._dir = Path(secrets_dir) if secrets_dir else DEFAULT_SECRETS_DIR

    def _resolve_path(self, path: str) -> Path:
        candidate = (self._dir / path)
        # Check containment without resolving symlinks — secrets are
        # commonly symlinked from external repos/vaults and .resolve()
        # would follow the link outside the secrets directory.
        normalized = Path(os.path.normpath(candidate))
        secrets_root = Path(os.path.normpath(self._dir))
        try:
            normalized.relative_to(secrets_root)
        except ValueError:
            raise ValueError(f"Secret path escapes secrets directory: {path}")
        return candidate

    async def get_secret(self, path: str) -> str | None:
        file_path = self._resolve_path(path)
        if not file_path.exists():
            logger.debug(f"Secret not found: {path}")
            return None
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.exception(f"Failed to read secret: {path}")
            return None

    async def get_secret_file(self, path: str) -> Path | None:
        file_path = self._resolve_path(path)
        if file_path.exists():
            return file_path
        return None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        search_dir = self._dir / prefix if prefix else self._dir
        if not search_dir.exists():
            return []
        return [
            str(f.relative_to(self._dir))
            for f in sorted(search_dir.rglob("*"))
            if f.is_file() or f.is_symlink()
        ]
