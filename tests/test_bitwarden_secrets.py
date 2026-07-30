"""Tests for BitwardenSecretsProvider.

All tests use a FakeSecretsManagerClient injected via ``_client`` — no
SDK import, no network, no optional extra needed.  The fake mimics the
SDK's ``secrets().list()`` and ``secrets().get()`` response shapes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from providers.secrets.base import SecretsBackendError
from providers.secrets.bitwarden import BitwardenSecretsProvider, resolve_access_token
from providers.secrets.cascade import CascadingSecretsProvider
from providers.secrets.local import LocalSecretsProvider

# ---------------------------------------------------------------------------
# Fake SDK client
# ---------------------------------------------------------------------------


@dataclass
class _SecretIdentifier:
    id: str
    key: str
    organization_id: str
    project_id: str


@dataclass
class _SecretValue:
    id: str
    key: str
    value: str
    note: str = ""


@dataclass
class _ListResponse:
    data: _ListResponseData


@dataclass
class _ListResponseData:
    data: list[_SecretIdentifier]


@dataclass
class _GetResponse:
    data: _SecretValue


class _FakeSecretsAPI:
    """Mimics ``client.secrets()`` from the Bitwarden SDK."""

    def __init__(
        self,
        secrets: dict[str, str],
        project_id: str = "proj-1",
        org_id: str = "org-1",
        *,
        error_on_list: Exception | None = None,
        error_on_get: Exception | None = None,
    ) -> None:
        self._project_id = project_id
        self._org_id = org_id
        self._error_on_list = error_on_list
        self._error_on_get = error_on_get

        self._identifiers: list[_SecretIdentifier] = []
        self._values: dict[str, _SecretValue] = {}
        for i, (key, value) in enumerate(secrets.items()):
            sid = f"secret-{i}"
            self._identifiers.append(
                _SecretIdentifier(
                    id=sid,
                    key=key,
                    organization_id=org_id,
                    project_id=project_id,
                )
            )
            self._values[sid] = _SecretValue(id=sid, key=key, value=value)

    def list(self, organization_id: str) -> _ListResponse:
        if self._error_on_list is not None:
            raise self._error_on_list
        return _ListResponse(
            data=_ListResponseData(data=list(self._identifiers)),
        )

    def get(self, secret_id: str) -> _GetResponse:
        if self._error_on_get is not None:
            raise self._error_on_get
        val = self._values.get(secret_id)
        if val is None:
            raise RuntimeError(f"Secret {secret_id} not found")
        return _GetResponse(data=val)


class _FakeClient:
    """Mimics the top-level ``BitwardenClient`` from the SDK."""

    def __init__(self, secrets_api: _FakeSecretsAPI) -> None:
        self._secrets_api = secrets_api

    def secrets(self) -> _FakeSecretsAPI:
        return self._secrets_api


def _make_provider(
    secrets: dict[str, str] | None = None,
    project_id: str = "proj-1",
    org_id: str = "org-1",
    cache_ttl: float = 60,
    clock_start: float = 1000.0,
    error_on_list: Exception | None = None,
    error_on_get: Exception | None = None,
) -> tuple[BitwardenSecretsProvider, list[float]]:
    """Build a provider with a fake client and controllable clock.

    Returns ``(provider, clock_ticks)`` — mutate ``clock_ticks[0]`` to
    advance the clock for cache-TTL tests.
    """
    clock_ticks = [clock_start]
    api = _FakeSecretsAPI(
        secrets or {},
        project_id=project_id,
        org_id=org_id,
        error_on_list=error_on_list,
        error_on_get=error_on_get,
    )
    client = _FakeClient(api)
    provider = BitwardenSecretsProvider(
        organization_id=org_id,
        project_id=project_id,
        access_token="fake-token",
        cache_ttl_seconds=cache_ttl,
        _client=client,
        _clock=lambda: clock_ticks[0],
    )
    return provider, clock_ticks


# ---------------------------------------------------------------------------
# get_secret tests
# ---------------------------------------------------------------------------


class TestGetSecret:
    async def test_hit_returns_value(self):
        provider, _ = _make_provider({"aws/config.json": '{"key": "val"}'})
        result = await provider.get_secret("aws/config.json")
        assert result == '{"key": "val"}'

    async def test_miss_returns_none(self):
        provider, _ = _make_provider({"aws/config.json": "val"})
        result = await provider.get_secret("nonexistent")
        assert result is None

    async def test_list_error_raises_backend_error(self):
        provider, _ = _make_provider(
            error_on_list=ConnectionError("network down"),
        )
        with pytest.raises(SecretsBackendError, match="network down"):
            await provider.get_secret("anything")

    async def test_get_error_raises_backend_error(self):
        provider, _ = _make_provider(
            {"token": "val"},
            error_on_get=TimeoutError("timed out"),
        )
        with pytest.raises(SecretsBackendError, match="timed out"):
            await provider.get_secret("token")

    async def test_filters_by_project_id(self):
        """Secrets from other projects are not visible."""
        api = _FakeSecretsAPI(
            {"shared-secret": "shared-val"},
            project_id="proj-1",
            org_id="org-1",
        )
        api._identifiers.append(
            _SecretIdentifier(
                id="other-secret-id",
                key="other-secret",
                organization_id="org-1",
                project_id="proj-2",
            )
        )
        client = _FakeClient(api)

        provider = BitwardenSecretsProvider(
            organization_id="org-1",
            project_id="proj-1",
            access_token="fake",
            _client=client,
            _clock=lambda: 1000.0,
        )
        assert await provider.get_secret("shared-secret") == "shared-val"
        assert await provider.get_secret("other-secret") is None


# ---------------------------------------------------------------------------
# Cache TTL tests
# ---------------------------------------------------------------------------


class TestCacheTTL:
    async def test_value_served_from_cache(self):
        provider, clock = _make_provider(
            {"token": "original"},
            cache_ttl=60,
        )
        assert await provider.get_secret("token") == "original"

        # Mutate the fake's backing store (simulating a vault-side rotation)
        api = provider._client.secrets()
        api._values["secret-0"].value = "rotated"

        # Still within TTL — cache hit
        clock[0] += 30
        assert await provider.get_secret("token") == "original"

    async def test_value_refreshed_after_ttl(self):
        provider, clock = _make_provider(
            {"token": "original"},
            cache_ttl=60,
        )
        assert await provider.get_secret("token") == "original"

        api = provider._client.secrets()
        api._values["secret-0"].value = "rotated"

        # Advance past TTL
        clock[0] += 61
        assert await provider.get_secret("token") == "rotated"

    async def test_list_cache_refreshed_after_ttl(self):
        provider, clock = _make_provider(
            {"existing": "val"},
            cache_ttl=60,
        )
        result = await provider.list_secrets()
        assert result == ["existing"]

        # Add a secret to the fake
        api = provider._client.secrets()
        api._identifiers.append(
            _SecretIdentifier(
                id="secret-new",
                key="new-secret",
                organization_id="org-1",
                project_id="proj-1",
            )
        )

        # Still cached
        clock[0] += 30
        assert "new-secret" not in await provider.list_secrets()

        # Past TTL — refresh picks up the new secret
        clock[0] += 31
        assert "new-secret" in await provider.list_secrets()


# ---------------------------------------------------------------------------
# get_secret_file + secret_file tests
# ---------------------------------------------------------------------------


class TestSecretFile:
    async def test_get_secret_file_always_none(self):
        provider, _ = _make_provider({"token": "val"})
        assert await provider.get_secret_file("token") is None

    async def test_secret_file_materializes_and_cleans_up(self):
        provider, _ = _make_provider({"ssh/id_rsa": "key-content"})
        materialized: Path | None = None

        async with provider.secret_file("ssh/id_rsa") as path:
            assert path is not None
            materialized = path
            assert path.exists()
            assert path.read_text() == "key-content"
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700

        assert materialized is not None
        assert not materialized.exists()

    async def test_secret_file_none_for_missing(self):
        provider, _ = _make_provider({})
        async with provider.secret_file("nonexistent") as path:
            assert path is None

    async def test_secret_file_cleans_up_on_exception(self):
        provider, _ = _make_provider({"token": "val"})
        materialized: Path | None = None

        with pytest.raises(RuntimeError, match="deliberate"):
            async with provider.secret_file("token") as path:
                materialized = path
                assert path is not None
                raise RuntimeError("deliberate")

        assert materialized is not None
        assert not materialized.exists()

    async def test_secret_file_uses_fixed_filename(self):
        """Filename is always 'secret' — never derived from the key."""
        provider, _ = _make_provider({"a/../b": "val"})
        async with provider.secret_file("a/../b") as path:
            assert path is not None
            assert path.name == "secret"


# ---------------------------------------------------------------------------
# list_secrets tests
# ---------------------------------------------------------------------------


class TestListSecrets:
    async def test_list_all(self):
        provider, _ = _make_provider(
            {"aws/key.json": "k1", "ssh/id_rsa": "k2"},
        )
        result = await provider.list_secrets()
        assert sorted(result) == ["aws/key.json", "ssh/id_rsa"]

    async def test_list_with_prefix(self):
        provider, _ = _make_provider(
            {"aws/key.json": "k1", "aws/config.json": "k2", "ssh/id_rsa": "k3"},
        )
        result = await provider.list_secrets("aws")
        assert sorted(result) == ["aws/config.json", "aws/key.json"]
        assert "ssh/id_rsa" not in result

    async def test_list_empty_project(self):
        provider, _ = _make_provider({})
        assert await provider.list_secrets() == []


# ---------------------------------------------------------------------------
# Token resolution tests
# ---------------------------------------------------------------------------


class TestTokenResolution:
    def test_env_var_takes_precedence(self, tmp_path):
        token_file = tmp_path / "access-token"
        token_file.write_text("file-token")

        with (
            patch.dict(os.environ, {_env_key(): "env-token"}),
            patch(
                "providers.secrets.bitwarden._ACCESS_TOKEN_FILE",
                token_file,
            ),
        ):
            assert resolve_access_token() == "env-token"

    def test_file_fallback(self, tmp_path):
        token_file = tmp_path / "access-token"
        token_file.write_text("  file-token  \n")

        with (
            patch.dict(os.environ, {}, clear=False),
            patch(
                "providers.secrets.bitwarden._ACCESS_TOKEN_FILE",
                token_file,
            ),
        ):
            os.environ.pop(_env_key(), None)
            assert resolve_access_token() == "file-token"

    def test_missing_raises(self, tmp_path):
        with (
            patch.dict(os.environ, {}, clear=False),
            patch(
                "providers.secrets.bitwarden._ACCESS_TOKEN_FILE",
                tmp_path / "nonexistent",
            ),
        ):
            os.environ.pop(_env_key(), None)
            with pytest.raises(SecretsBackendError, match="not found"):
                resolve_access_token()


def _env_key() -> str:
    return "AGENTIC_PERF_BWS_TOKEN"


# ---------------------------------------------------------------------------
# Import guard test
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_constructor_raises_without_sdk(self):
        with patch.dict("sys.modules", {"bitwarden_sdk": None}):
            with pytest.raises(ImportError, match="bitwarden-sdk"):
                BitwardenSecretsProvider(
                    organization_id="org-1",
                    project_id="proj-1",
                    access_token="fake-token",
                )


# ---------------------------------------------------------------------------
# Cascade error propagation test
# ---------------------------------------------------------------------------


class TestCascadeErrorPropagation:
    """Pin the behavior that SecretsBackendError propagates through cascades."""

    async def test_backend_error_propagates(self, tmp_path):
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "token").write_text("local-value")

        vault_provider, _ = _make_provider(
            error_on_list=ConnectionError("vault unreachable"),
        )
        local_provider = LocalSecretsProvider(shared_dir)

        cascade = CascadingSecretsProvider(
            [
                ("vault:shared", vault_provider),
                ("shared", local_provider),
            ]
        )

        with pytest.raises(SecretsBackendError, match="vault unreachable"):
            await cascade.get_secret("token")
