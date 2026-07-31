"""Tests for vault-fallback SSH key resolution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from providers.secrets.base import SecretsProvider
from providers.ssh import SSHKeyResolutionError


class FakeVaultProvider(SecretsProvider):
    """Vault-like provider that materializes secrets to temp files."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self.get_secret_calls: list[str] = []

    async def get_secret(self, path: str) -> str | None:
        self.get_secret_calls.append(path)
        return self._secrets.get(path)

    async def get_secret_file(self, path: str) -> Path | None:
        return None

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        content = self._secrets.get(path)
        if content is None:
            yield None
            return
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp())
        tmp = tmp_dir / "key"
        tmp.write_text(content)
        tmp.chmod(0o600)
        try:
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)
            tmp_dir.rmdir()

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return [k for k in self._secrets if k.startswith(prefix)]


SSH_KEY_CONTENT = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-data\n-----END OPENSSH PRIVATE KEY-----\n"


class TestResolveSSHKey:
    async def test_file_exists_yields_path(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        key_file = tmp_path / "id_ed25519"
        key_file.write_text(SSH_KEY_CONTENT)

        provider = FakeVaultProvider({"ssh/key": SSH_KEY_CONTENT})

        async with resolve_ssh_key(str(key_file), provider, "ssh/key") as resolved:
            assert resolved == str(key_file)

        assert provider.get_secret_calls == []

    async def test_tilde_expansion(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        key_file = tmp_path / "test-key"
        key_file.write_text(SSH_KEY_CONTENT)

        with patch("agents.server_utils.Path.expanduser", return_value=key_file):
            async with resolve_ssh_key("~/test-key", None, None) as resolved:
                assert resolved == str(key_file)

    async def test_missing_file_vault_found(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        missing = str(tmp_path / "nonexistent")
        provider = FakeVaultProvider({"ssh/id_ed25519": SSH_KEY_CONTENT})

        async with resolve_ssh_key(missing, provider, "ssh/id_ed25519") as resolved:
            assert resolved is not None
            resolved_path = Path(resolved)
            assert resolved_path.exists()
            assert resolved_path.read_text() == SSH_KEY_CONTENT
            mode = resolved_path.stat().st_mode
            assert mode & 0o777 == 0o600

    async def test_cleanup_on_exit(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        missing = str(tmp_path / "nonexistent")
        provider = FakeVaultProvider({"ssh/key": SSH_KEY_CONTENT})

        materialized_path: str | None = None
        async with resolve_ssh_key(missing, provider, "ssh/key") as resolved:
            materialized_path = resolved

        assert materialized_path is not None
        assert not Path(materialized_path).exists()

    async def test_missing_file_vault_configured_not_found(
        self, tmp_path: Path
    ) -> None:
        from agents.server_utils import resolve_ssh_key

        missing = str(tmp_path / "nonexistent")
        provider = FakeVaultProvider({})

        with pytest.raises(SSHKeyResolutionError, match="not found"):
            async with resolve_ssh_key(missing, provider, "ssh/missing") as _:
                pass

    async def test_no_vault_config_yields_original(self) -> None:
        from agents.server_utils import resolve_ssh_key

        async with resolve_ssh_key("/some/path", None, None) as resolved:
            assert resolved == "/some/path"

    async def test_none_ssh_key_no_vault(self) -> None:
        from agents.server_utils import resolve_ssh_key

        async with resolve_ssh_key(None, None, None) as resolved:
            assert resolved is None

    async def test_none_ssh_key_vault_found(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        provider = FakeVaultProvider({"ssh/key": SSH_KEY_CONTENT})

        async with resolve_ssh_key(None, provider, "ssh/key") as resolved:
            assert resolved is not None
            assert Path(resolved).read_text() == SSH_KEY_CONTENT

    async def test_empty_string_ssh_key_vault_found(self, tmp_path: Path) -> None:
        from agents.server_utils import resolve_ssh_key

        provider = FakeVaultProvider({"ssh/key": SSH_KEY_CONTENT})

        async with resolve_ssh_key("", provider, "ssh/key") as resolved:
            assert resolved is not None
            assert Path(resolved).read_text() == SSH_KEY_CONTENT


class TestResolveVaultSecretName:
    def test_ticket_field_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.server_utils import _resolve_vault_secret_name

        monkeypatch.setenv("SSH_KEY_VAULT_SECRET", "env-secret")

        result = _resolve_vault_secret_name({"ssh_key_secret": "ticket-secret"})
        assert result == "ticket-secret"

    def test_env_var_wins_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.server_utils import _resolve_vault_secret_name

        monkeypatch.setenv("SSH_KEY_VAULT_SECRET", "env-secret")

        with patch(
            "agents.server_utils._load_config_value",
            return_value="config-secret",
        ):
            result = _resolve_vault_secret_name({})
            assert result == "env-secret"

    def test_config_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.server_utils import _resolve_vault_secret_name

        monkeypatch.delenv("SSH_KEY_VAULT_SECRET", raising=False)

        with patch(
            "agents.server_utils._load_config_value",
            return_value="config-secret",
        ):
            result = _resolve_vault_secret_name({})
            assert result == "config-secret"

    def test_all_empty_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.server_utils import _resolve_vault_secret_name

        monkeypatch.delenv("SSH_KEY_VAULT_SECRET", raising=False)

        with patch(
            "agents.server_utils._load_config_value",
            return_value=None,
        ):
            result = _resolve_vault_secret_name({})
            assert result is None

    def test_no_ticket_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.server_utils import _resolve_vault_secret_name

        monkeypatch.setenv("SSH_KEY_VAULT_SECRET", "env-secret")

        result = _resolve_vault_secret_name(None)
        assert result == "env-secret"
