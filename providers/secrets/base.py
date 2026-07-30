from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class SecretsProvider(ABC):
    @abstractmethod
    async def get_secret(self, path: str) -> str | None:
        """Read a secret value by path. Returns the content as a string, or None."""
        ...

    @abstractmethod
    async def get_secret_file(self, path: str) -> Path | None:
        """Get the filesystem path to a secret file (for SCP to remote hosts).
        Returns None if the secret doesn't exist or isn't file-backed.

        Prefer ``secret_file()`` for transient consumers (SCP, subprocess).
        This method remains for providers that expose persistent file paths.
        """
        ...

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        """Yield a filesystem path for the secret, valid inside the context only.

        This is the preferred API for consumers that need a file transiently
        (SCP, subprocess argument).  The default yields ``get_secret_file()``
        with no cleanup — correct for file-backed providers.  Non-file-backed
        providers (e.g. vault) override this to materialize an ephemeral file
        and clean it up on exit.
        """
        yield await self.get_secret_file(path)

    @abstractmethod
    async def list_secrets(self, prefix: str = "") -> list[str]:
        """List available secret paths under the given prefix."""
        ...
