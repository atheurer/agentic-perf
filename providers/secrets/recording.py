from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from providers.redaction import Redactor
from providers.secrets.base import SecretsProvider


class RecordingSecretsProvider(SecretsProvider):
    """Wrapper that registers secret values with a Redactor on access.

    Intercepts ``get_secret()`` — the only method that reveals
    plaintext values — and registers each non-None result with the
    ticket's redaction registry. All other methods delegate unchanged.
    """

    def __init__(
        self,
        inner: SecretsProvider,
        redactor: Redactor,
        ticket_id: str,
    ) -> None:
        self._inner = inner
        self._redactor = redactor
        self._ticket_id = ticket_id

    async def get_secret(self, path: str) -> str | None:
        value = await self._inner.get_secret(path)
        if value is not None:
            self._redactor.register(self._ticket_id, path, value)
        return value

    async def get_secret_file(self, path: str) -> Path | None:
        return await self._inner.get_secret_file(path)

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        async with self._inner.secret_file(path) as p:
            yield p

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return await self._inner.list_secrets(prefix)
