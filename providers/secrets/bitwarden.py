"""Bitwarden Secrets Manager provider.

Reads secrets from Bitwarden Secrets Manager via the official Python SDK
(``bitwarden-sdk``, optional extra ``[bitwarden]``).  The SDK is imported
lazily in the constructor so that importing this module never fails — only
constructing a real client without the extra installed raises.

Secrets are identified by their key name within a Secrets Manager project.
A secret named ``aws/config.json`` in the configured project serves
``get_secret("aws/config.json")``.

Vault secrets have no persistent file on disk.  ``get_secret_file()``
always returns ``None``; use ``secret_file()`` for transient file access —
it materializes an ephemeral 0600 file in a private tmpdir and removes
it on context exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .base import SecretsBackendError, SecretsProvider

logger = logging.getLogger(__name__)

_ACCESS_TOKEN_ENV = "AGENTIC_PERF_BWS_TOKEN"
_ACCESS_TOKEN_FILE = (
    Path.home() / ".agentic-perf" / "secrets" / "bitwarden" / "access-token"
)


def resolve_access_token() -> str:
    """Resolve the Bitwarden SM access token.

    Order: env ``AGENTIC_PERF_BWS_TOKEN`` > file
    ``~/.agentic-perf/secrets/bitwarden/access-token`` (0600).
    """
    env_token = os.environ.get(_ACCESS_TOKEN_ENV)
    if env_token:
        return env_token

    if _ACCESS_TOKEN_FILE.exists():
        return _ACCESS_TOKEN_FILE.read_text(encoding="utf-8").strip()

    raise SecretsBackendError(
        f"Bitwarden access token not found.  Set {_ACCESS_TOKEN_ENV} or "
        f"place it in {_ACCESS_TOKEN_FILE} (mode 0600)."
    )


class _CachedValue:
    __slots__ = ("value", "fetched_at")

    def __init__(self, value: str, fetched_at: float) -> None:
        self.value = value
        self.fetched_at = fetched_at


class BitwardenSecretsProvider(SecretsProvider):
    """Secrets provider backed by Bitwarden Secrets Manager.

    Parameters
    ----------
    organization_id:
        Bitwarden organization UUID.  Required by the SDK for listing.
    project_id:
        Secrets Manager project UUID scoping this provider instance.
    access_token:
        Machine-account access token for authentication.
    server_url:
        Self-hosted server base URL (e.g. ``https://vault.example.com``).
        Omit for Bitwarden cloud.
    cache_ttl_seconds:
        In-memory TTL for secret reads and listings.  A rotated vault
        secret takes up to this long to appear.  Default 60 s.
    _client:
        Injected SDK client for testing — skips SDK import and auth.
    _clock:
        Injected monotonic clock for deterministic cache-TTL testing.
    """

    def __init__(
        self,
        organization_id: str,
        project_id: str,
        access_token: str,
        server_url: str | None = None,
        cache_ttl_seconds: float = 60,
        *,
        _client: Any = None,
        _clock: Callable[[], float] | None = None,
    ) -> None:
        self._organization_id = organization_id
        self._project_id = project_id
        self._cache_ttl = cache_ttl_seconds
        self._clock = _clock or time.monotonic

        if _client is not None:
            self._client = _client
        else:
            try:
                from bitwarden_sdk import (  # type: ignore[import-untyped]
                    BitwardenClient,
                    DeviceType,
                    client_settings_from_dict,
                )
            except ImportError:
                raise ImportError(
                    "bitwarden-sdk is required for vault secrets.  "
                    "Install with:  pip install 'agentic-perf[bitwarden]'"
                ) from None

            settings: dict[str, Any] = {
                "deviceType": DeviceType.SDK,
                "userAgent": "agentic-perf",
            }
            if server_url:
                base = server_url.rstrip("/")
                settings["apiUrl"] = f"{base}/api"
                settings["identityUrl"] = f"{base}/identity"
            self._client = BitwardenClient(
                client_settings_from_dict(settings),
            )
            self._client.auth().login_access_token(access_token)

        # key → secret-id mapping (from list), refreshed on TTL expiry
        self._key_map: dict[str, str] = {}
        self._key_map_time: float | None = None

        # key → cached value (from get), per-key TTL
        self._value_cache: dict[str, _CachedValue] = {}

        # Serialize cache refreshes to prevent concurrent stampedes
        self._refresh_lock = asyncio.Lock()

    def _is_fresh(self, fetched_at: float | None) -> bool:
        if fetched_at is None:
            return False
        return (self._clock() - fetched_at) < self._cache_ttl

    async def _refresh_key_map(self) -> None:
        """Refresh the key → secret-id mapping from the SM project."""
        try:
            response = await asyncio.to_thread(
                self._client.secrets().list,
                self._organization_id,
            )
            new_map: dict[str, str] = {}
            for item in response.data.data:
                if str(getattr(item, "project_id", "")) == self._project_id:
                    new_map[item.key] = str(item.id)
        except SecretsBackendError:
            raise
        except Exception as exc:
            raise SecretsBackendError(f"Failed to list secrets: {exc}") from exc

        self._key_map = new_map
        self._key_map_time = self._clock()

    async def _ensure_key_map(self) -> None:
        if self._is_fresh(self._key_map_time):
            return
        async with self._refresh_lock:
            if self._is_fresh(self._key_map_time):
                return
            await self._refresh_key_map()

    async def get_secret(self, path: str) -> str | None:
        cached = self._value_cache.get(path)
        if cached is not None and self._is_fresh(cached.fetched_at):
            return cached.value

        await self._ensure_key_map()
        secret_id = self._key_map.get(path)
        if secret_id is None:
            return None

        try:
            response = await asyncio.to_thread(
                self._client.secrets().get,
                secret_id,
            )
            value = response.data.value
        except SecretsBackendError:
            raise
        except Exception as exc:
            raise SecretsBackendError(
                f"Failed to read secret '{path}': {exc}",
            ) from exc

        self._value_cache[path] = _CachedValue(value, self._clock())
        return value

    async def get_secret_file(self, path: str) -> Path | None:
        logger.debug(
            "get_secret_file('%s') on vault provider returns None — "
            "use secret_file() for transient file access",
            path,
        )
        return None

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        content = await self.get_secret(path)
        if content is None:
            yield None
            return

        tmp_dir = Path(tempfile.mkdtemp(prefix="bws-"))
        try:
            tmp_dir.chmod(0o700)
            tmp_file = tmp_dir / "secret"
            tmp_file.write_text(content, encoding="utf-8")
            tmp_file.chmod(0o600)
            yield tmp_file
        finally:
            try:
                for child in tmp_dir.iterdir():
                    child.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                logger.warning(
                    "Could not remove ephemeral dir %s",
                    tmp_dir,
                )

    async def list_secrets(self, prefix: str = "") -> list[str]:
        await self._ensure_key_map()
        if not prefix:
            return list(self._key_map.keys())
        return [k for k in self._key_map if k.startswith(prefix)]
