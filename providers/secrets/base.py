from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SecretsProvider(ABC):
    @abstractmethod
    async def get_secret(self, path: str) -> str | None:
        """Read a secret value by path. Returns the content as a string, or None."""
        ...

    @abstractmethod
    async def get_secret_file(self, path: str) -> Path | None:
        """Get the filesystem path to a secret file (for SCP to remote hosts).
        Returns None if the secret doesn't exist or isn't file-backed."""
        ...

    @abstractmethod
    async def list_secrets(self, prefix: str = "") -> list[str]:
        """List available secret paths under the given prefix."""
        ...
