"""Tests for the cascading secrets provider and per-ticket resolution."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from providers.secrets.base import SecretsBackendError
from providers.secrets.bitwarden import BitwardenSecretsProvider
from providers.secrets.cascade import (
    CascadingSecretsProvider,
    build_cascade_for_user,
)
from providers.secrets.local import LocalSecretsProvider


def _write_secret(base, path, content="secret-value"):
    full = base / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


def _make_fake_vault(
    secrets: dict[str, str],
    project_id: str = "proj-1",
    org_id: str = "org-1",
    error_on_list: Exception | None = None,
) -> BitwardenSecretsProvider:
    """Build a BitwardenSecretsProvider with fake client for cascade tests."""
    from tests.test_bitwarden_secrets import _FakeClient, _FakeSecretsAPI

    api = _FakeSecretsAPI(
        secrets,
        project_id=project_id,
        org_id=org_id,
        error_on_list=error_on_list,
    )
    client = _FakeClient(api)
    return BitwardenSecretsProvider(
        organization_id=org_id,
        project_id=project_id,
        access_token="fake-token",
        _client=client,
        _clock=lambda: 1000.0,
    )


class TestCascadingSecretsProvider:
    @pytest.fixture()
    def secrets_root(self, tmp_path):
        return tmp_path / "secrets"

    async def test_first_layer_wins(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        _write_secret(user_dir, "aws/key.json", "user-key")
        _write_secret(shared_dir, "aws/key.json", "shared-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret("aws/key.json")
        assert result == "user-key"

    async def test_fallback_to_later_layer(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        user_dir.mkdir(parents=True, exist_ok=True)
        _write_secret(shared_dir, "aws/key.json", "shared-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret("aws/key.json")
        assert result == "shared-key"

    async def test_returns_none_when_missing(self, secrets_root):
        shared_dir = secrets_root / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret("nonexistent")
        assert result is None

    async def test_shadow_detection_logs(self, secrets_root, caplog):
        user_dir = secrets_root / "user"
        group_dir = secrets_root / "group"
        _write_secret(user_dir, "aws/key.json", "user-key")
        _write_secret(group_dir, "aws/key.json", "group-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("group:devs", LocalSecretsProvider(group_dir)),
            ]
        )

        with caplog.at_level(logging.INFO, logger="providers.secrets.cascade"):
            result = await cascade.get_secret("aws/key.json")

        assert result == "user-key"
        assert "shadowed" in caplog.text
        assert "group:devs" in caplog.text
        assert "user:alice" in caplog.text

    async def test_get_secret_file_cascade(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        _write_secret(user_dir, "ssh/id_rsa", "user-ssh")
        _write_secret(shared_dir, "ssh/id_rsa", "shared-ssh")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret_file("ssh/id_rsa")
        assert result is not None
        assert "user" in str(result)

    async def test_get_secret_file_fallback(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        user_dir.mkdir(parents=True, exist_ok=True)
        _write_secret(shared_dir, "ssh/id_rsa", "shared-ssh")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret_file("ssh/id_rsa")
        assert result is not None
        assert "shared" in str(result)

    async def test_get_secret_file_missing(self, secrets_root):
        shared_dir = secrets_root / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.get_secret_file("nonexistent")
        assert result is None

    async def test_list_secrets_dedup(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        _write_secret(user_dir, "aws/key.json", "user-key")
        _write_secret(shared_dir, "aws/key.json", "shared-key")
        _write_secret(shared_dir, "ssh/id_rsa", "shared-ssh")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.list_secrets()
        assert "aws/key.json" in result
        assert "ssh/id_rsa" in result
        assert result.count("aws/key.json") == 1

    async def test_list_secrets_with_prefix(self, secrets_root):
        shared_dir = secrets_root / "shared"
        _write_secret(shared_dir, "aws/key.json", "k1")
        _write_secret(shared_dir, "aws/config.json", "k2")
        _write_secret(shared_dir, "ssh/id_rsa", "k3")

        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        result = await cascade.list_secrets("aws")
        assert "aws/key.json" in result
        assert "aws/config.json" in result
        assert "ssh/id_rsa" not in result

    def test_empty_layers_rejected(self):
        with pytest.raises(ValueError, match="at least one layer"):
            CascadingSecretsProvider([])


class TestBuildCascadeForUser:
    @pytest.fixture()
    def secrets_root(self, tmp_path):
        root = tmp_path / "secrets"
        root.mkdir()
        return root

    async def test_user_layer_included(self, secrets_root):
        user_dir = secrets_root / "users" / "alice"
        _write_secret(user_dir, "api-key", "alice-key")
        _write_secret(secrets_root, "api-key", "shared-key")

        cascade = build_cascade_for_user("alice", [], secrets_root)

        result = await cascade.get_secret("api-key")
        assert result == "alice-key"

    async def test_group_layer_between_user_and_shared(self, secrets_root):
        group_dir = secrets_root / "groups" / "gpu-team"
        _write_secret(group_dir, "nvidia/license", "team-license")
        _write_secret(secrets_root, "nvidia/license", "shared-license")

        cascade = build_cascade_for_user("bob", ["gpu-team"], secrets_root)

        result = await cascade.get_secret("nvidia/license")
        assert result == "team-license"

    async def test_multiple_groups_alpha_ordered(self, secrets_root):
        _write_secret(
            secrets_root / "groups" / "b-team",
            "config.json",
            "b-team-config",
        )
        _write_secret(
            secrets_root / "groups" / "a-team",
            "config.json",
            "a-team-config",
        )

        cascade = build_cascade_for_user(
            "charlie",
            ["b-team", "a-team"],
            secrets_root,
        )

        result = await cascade.get_secret("config.json")
        assert result == "a-team-config"

    async def test_user_over_group(self, secrets_root):
        _write_secret(
            secrets_root / "users" / "alice",
            "token",
            "alice-token",
        )
        _write_secret(
            secrets_root / "groups" / "devs",
            "token",
            "devs-token",
        )
        _write_secret(secrets_root, "token", "shared-token")

        cascade = build_cascade_for_user(
            "alice",
            ["devs"],
            secrets_root,
        )

        result = await cascade.get_secret("token")
        assert result == "alice-token"

    async def test_missing_user_dir_skipped(self, secrets_root):
        _write_secret(secrets_root, "api-key", "shared")

        cascade = build_cascade_for_user("ghost", [], secrets_root)

        result = await cascade.get_secret("api-key")
        assert result == "shared"

    async def test_missing_group_dir_skipped(self, secrets_root):
        _write_secret(secrets_root, "api-key", "shared")

        cascade = build_cascade_for_user(
            "alice",
            ["nonexistent-group"],
            secrets_root,
        )

        result = await cascade.get_secret("api-key")
        assert result == "shared"

    async def test_shared_only_when_no_overrides(self, secrets_root):
        _write_secret(secrets_root, "token", "shared-token")

        cascade = build_cascade_for_user("alice", [], secrets_root)

        result = await cascade.get_secret("token")
        assert result == "shared-token"

    async def test_containment_per_layer(self, secrets_root):
        user_dir = secrets_root / "users" / "alice"
        user_dir.mkdir(parents=True, exist_ok=True)

        cascade = build_cascade_for_user("alice", [], secrets_root)

        with pytest.raises(ValueError, match="escapes"):
            await cascade.get_secret("../../etc/passwd")


class TestDispatcherSecrets:
    @pytest.fixture()
    def secrets_root(self, tmp_path):
        root = tmp_path / "secrets"
        root.mkdir()
        _write_secret(root, "aws/config.json", '{"shared": true}')
        _write_secret(
            root / "users" / "alice",
            "aws/config.json",
            '{"alice": true}',
        )
        return root

    def _make_dispatcher(self, secrets_root, user_store=None):
        from orchestrator.dispatcher import Dispatcher

        shared = LocalSecretsProvider(secrets_root)
        return Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=shared,
            user_store=user_store,
            secrets_root=secrets_root if user_store else None,
        ), shared

    def test_returns_cascade_for_known_user(self, secrets_root):
        from state_store.identity import UserStore

        user_store = UserStore(persist_path=secrets_root / "users.json")
        user_store.create_user("alice")

        dispatcher, _ = self._make_dispatcher(secrets_root, user_store)
        secrets = dispatcher._get_secrets_for_ticket({"created_by": "alice"})
        assert isinstance(secrets, CascadingSecretsProvider)

    def test_returns_shared_for_unclaimed(self, secrets_root):
        from state_store.identity import UserStore

        user_store = UserStore(persist_path=secrets_root / "users.json")
        dispatcher, shared = self._make_dispatcher(secrets_root, user_store)

        result = dispatcher._get_secrets_for_ticket({"created_by": ""})
        assert result is shared

    def test_returns_shared_in_legacy_mode(self, secrets_root):
        from orchestrator.dispatcher import Dispatcher

        shared = LocalSecretsProvider(secrets_root)
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=shared,
        )

        result = dispatcher._get_secrets_for_ticket({"created_by": "alice"})
        assert result is shared

    def test_returns_shared_for_unknown_user(self, secrets_root):
        from state_store.identity import UserStore

        user_store = UserStore(persist_path=secrets_root / "users.json")
        dispatcher, shared = self._make_dispatcher(secrets_root, user_store)

        result = dispatcher._get_secrets_for_ticket({"created_by": "ghost"})
        assert result is shared

    def test_returns_shared_for_none_ticket(self, secrets_root):
        from orchestrator.dispatcher import Dispatcher

        shared = LocalSecretsProvider(secrets_root)
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=shared,
        )

        result = dispatcher._get_secrets_for_ticket(None)
        assert result is shared

    def test_cascade_includes_user_groups(self, secrets_root):
        from state_store.identity import UserStore

        _write_secret(
            secrets_root / "groups" / "devs",
            "group-secret",
            "devs-value",
        )

        user_store = UserStore(persist_path=secrets_root / "users.json")
        user_store.create_user("alice")
        user_store.create_group("devs")
        user_store.add_member("devs", "alice")

        dispatcher, _ = self._make_dispatcher(secrets_root, user_store)
        secrets = dispatcher._get_secrets_for_ticket({"created_by": "alice"})
        assert isinstance(secrets, CascadingSecretsProvider)

    async def test_shared_layer_excludes_users_and_groups(self, secrets_root):
        from state_store.identity import UserStore

        _write_secret(secrets_root / "users" / "bob", "user-secret", "bob-value")
        _write_secret(secrets_root / "groups" / "gpu-team", "group-secret", "gpu-value")

        user_store = UserStore(persist_path=secrets_root / "users.json")
        user_store.create_user("alice")

        dispatcher, _ = self._make_dispatcher(secrets_root, user_store)
        secrets = dispatcher._get_secrets_for_ticket({"created_by": "alice"})

        # Try to read Bob's user secret or the GPU team's secret via the cascade's shared fallback layer.
        # Since 'alice' does not have these, the cascade falls back to the 'shared' layer.
        # But 'shared' excludes 'users' and 'groups' subfolders, so it should raise a ValueError.
        with pytest.raises(ValueError, match="restricted"):
            await secrets.get_secret("users/bob/user-secret")

        with pytest.raises(ValueError, match="restricted"):
            await secrets.get_secret("groups/gpu-team/group-secret")


class TestSecretFileContract:
    """Tests for the secret_file() async context manager contract."""

    @pytest.fixture()
    def secrets_root(self, tmp_path):
        return tmp_path / "secrets"

    async def test_local_secret_file_yields_same_as_get_secret_file(
        self,
        secrets_root,
    ):
        _write_secret(secrets_root, "ssh/id_rsa", "key-content")
        provider = LocalSecretsProvider(secrets_root)

        expected = await provider.get_secret_file("ssh/id_rsa")
        async with provider.secret_file("ssh/id_rsa") as actual:
            assert actual == expected
            assert actual is not None
            assert actual.exists()

    async def test_local_secret_file_yields_none_for_missing(self, secrets_root):
        secrets_root.mkdir(parents=True, exist_ok=True)
        provider = LocalSecretsProvider(secrets_root)

        async with provider.secret_file("nonexistent") as path:
            assert path is None

    async def test_cascade_secret_file_first_layer_wins(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        _write_secret(user_dir, "ssh/id_rsa", "user-key")
        _write_secret(shared_dir, "ssh/id_rsa", "shared-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        async with cascade.secret_file("ssh/id_rsa") as path:
            assert path is not None
            assert "user" in str(path)

    async def test_cascade_secret_file_fallback(self, secrets_root):
        user_dir = secrets_root / "user"
        shared_dir = secrets_root / "shared"
        user_dir.mkdir(parents=True, exist_ok=True)
        _write_secret(shared_dir, "ssh/id_rsa", "shared-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        async with cascade.secret_file("ssh/id_rsa") as path:
            assert path is not None
            assert "shared" in str(path)

    async def test_cascade_secret_file_missing(self, secrets_root):
        shared_dir = secrets_root / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(shared_dir)),
            ]
        )

        async with cascade.secret_file("nonexistent") as path:
            assert path is None

    async def test_cascade_secret_file_shadow_logged(
        self,
        secrets_root,
        caplog,
    ):
        user_dir = secrets_root / "user"
        group_dir = secrets_root / "group"
        _write_secret(user_dir, "ssh/id_rsa", "user-key")
        _write_secret(group_dir, "ssh/id_rsa", "group-key")

        cascade = CascadingSecretsProvider(
            [
                ("user:alice", LocalSecretsProvider(user_dir)),
                ("group:devs", LocalSecretsProvider(group_dir)),
            ]
        )

        with caplog.at_level(logging.INFO, logger="providers.secrets.cascade"):
            async with cascade.secret_file("ssh/id_rsa") as path:
                assert path is not None

        assert "shadowed" in caplog.text
        assert "group:devs" in caplog.text
        assert "user:alice" in caplog.text


class _EphemeralProvider(LocalSecretsProvider):
    """Fake provider simulating a non-file-backed backend (like a vault).

    ``get_secret_file()`` always returns None (no persistent file).
    ``secret_file()`` materializes a temporary file inside the context.
    Tracks enter/exit calls for lifecycle assertions.
    """

    def __init__(self, secrets_dir, tmp_root: Path, **kwargs):
        super().__init__(secrets_dir, **kwargs)
        self._tmp_root = tmp_root
        self.entered: list[str] = []
        self.exited: list[str] = []

    async def get_secret_file(self, path: str) -> Path | None:
        return None

    @asynccontextmanager
    async def secret_file(self, path: str) -> AsyncIterator[Path | None]:
        content = await self.get_secret(path)
        if content is None:
            yield None
            return
        self.entered.append(path)
        tmp_dir = self._tmp_root / f"ephemeral-{len(self.entered)}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = tmp_dir / path.replace("/", "_")
        try:
            tmp_file.write_text(content)
            tmp_file.chmod(0o600)
            yield tmp_file
        finally:
            tmp_file.unlink(missing_ok=True)
            tmp_dir.rmdir()
            self.exited.append(path)


class TestBinaryFileProbe:
    """Binary files should be found by cascade secret_file() via get_secret_file() fallback."""

    async def test_binary_file_found_through_cascade(self, tmp_path):
        root = tmp_path / "secrets"
        root.mkdir()
        binary_path = root / "cert.p12"
        binary_path.write_bytes(b"\x00\x01\x02\x03\xff\xfe")

        cascade = CascadingSecretsProvider([("shared", LocalSecretsProvider(root))])

        async with cascade.secret_file("cert.p12") as path:
            assert path is not None
            assert path.read_bytes() == b"\x00\x01\x02\x03\xff\xfe"

    async def test_binary_local_beats_vault(self, tmp_path):
        """Local binary file should win over vault text secret."""
        root = tmp_path / "secrets"
        root.mkdir()
        (root / "cert.p12").write_bytes(b"\x00\x01\x02\xff")

        vault = _make_fake_vault({"cert.p12": "vault-text-version"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        async with cascade.secret_file("cert.p12") as path:
            assert path is not None
            assert path.read_bytes() == b"\x00\x01\x02\xff"


class TestSecretFileEphemeralProvider:
    """Tests demonstrating the vault-like ephemeral provider pattern.

    These tests document the known limitation that the current cascade
    probes with get_secret_file() — ephemeral providers whose
    get_secret_file() returns None cannot win cascade selection.
    PR 3 updates the cascade probing when vault layers are wired in.
    """

    @pytest.fixture()
    def secrets_root(self, tmp_path):
        root = tmp_path / "secrets"
        _write_secret(root, "token", "ephemeral-value")
        return root

    async def test_ephemeral_provider_direct_use(self, secrets_root, tmp_path):
        provider = _EphemeralProvider(secrets_root, tmp_root=tmp_path / "eph")

        assert await provider.get_secret_file("token") is None

        async with provider.secret_file("token") as path:
            assert path is not None
            assert path.exists()
            assert path.read_text() == "ephemeral-value"
            assert path.stat().st_mode & 0o777 == 0o600

        assert not path.exists()
        assert provider.entered == ["token"]
        assert provider.exited == ["token"]

    async def test_ephemeral_provider_cleans_up_on_exception(
        self,
        secrets_root,
        tmp_path,
    ):
        provider = _EphemeralProvider(secrets_root, tmp_root=tmp_path / "eph")
        materialized_path = None

        with pytest.raises(RuntimeError, match="deliberate"):
            async with provider.secret_file("token") as path:
                materialized_path = path
                assert path is not None
                raise RuntimeError("deliberate")

        assert not materialized_path.exists()
        assert provider.exited == ["token"]

    async def test_cascade_secret_file_finds_ephemeral_layers(
        self,
        secrets_root,
        tmp_path,
    ):
        """Cascade probes via get_secret() so vault-like layers win."""
        ephemeral = _EphemeralProvider(
            secrets_root,
            tmp_root=tmp_path / "eph",
        )

        cascade = CascadingSecretsProvider([("vault:shared", ephemeral)])

        async with cascade.secret_file("token") as path:
            assert path is not None
            assert path.read_text() == "ephemeral-value"

        assert ephemeral.entered == ["token"]
        assert ephemeral.exited == ["token"]


# ---------------------------------------------------------------------------
# PR 3 — vault layer wiring tests
# ---------------------------------------------------------------------------


class TestCascadeWithVaultLayers:
    """Cascade with mixed local + vault (Bitwarden) layers."""

    async def test_local_beats_vault_within_tier(self, tmp_path):
        """Local file on disk takes precedence over vault in same tier."""
        root = tmp_path / "secrets"
        _write_secret(root, "token", "local-value")

        vault = _make_fake_vault({"token": "vault-value"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        result = await cascade.get_secret("token")
        assert result == "local-value"

    async def test_vault_fills_in_for_missing_local(self, tmp_path):
        """Vault provides secrets that don't exist on disk."""
        root = tmp_path / "secrets"
        root.mkdir(parents=True)

        vault = _make_fake_vault({"cloud/api-key": "vault-key"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        assert await cascade.get_secret("cloud/api-key") == "vault-key"

    async def test_vault_shadow_logging(self, tmp_path, caplog):
        """Shadow detection fires when local masks vault."""
        root = tmp_path / "secrets"
        _write_secret(root, "token", "local-value")

        vault = _make_fake_vault({"token": "vault-value"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        with caplog.at_level(logging.INFO):
            await cascade.get_secret("token")

        assert "shadowed" in caplog.text
        assert "vault:shared" in caplog.text

    async def test_vault_secret_file_materializes(self, tmp_path):
        """Vault secrets are materialized via secret_file() in cascade."""
        root = tmp_path / "secrets"
        root.mkdir(parents=True)

        vault = _make_fake_vault({"ssh/id_rsa": "key-material"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        async with cascade.secret_file("ssh/id_rsa") as path:
            assert path is not None
            assert path.read_text() == "key-material"
            assert path.stat().st_mode & 0o777 == 0o600

        assert not path.exists()

    async def test_local_secret_file_wins_over_vault(self, tmp_path):
        """When local has the file, secret_file() yields local's path."""
        root = tmp_path / "secrets"
        _write_secret(root, "token", "local-value")

        vault = _make_fake_vault({"token": "vault-value"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        async with cascade.secret_file("token") as path:
            assert path is not None
            assert path.read_text() == "local-value"
            assert path == root / "token"

    async def test_vault_error_propagates_through_cascade(self, tmp_path):
        """SecretsBackendError from vault propagates, not silently masked."""
        root = tmp_path / "secrets"
        root.mkdir(parents=True)

        vault = _make_fake_vault(
            {},
            error_on_list=ConnectionError("vault down"),
        )
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        with pytest.raises(SecretsBackendError, match="vault down"):
            await cascade.get_secret("anything")

    async def test_list_secrets_includes_vault(self, tmp_path):
        """list_secrets() merges local and vault keys."""
        root = tmp_path / "secrets"
        _write_secret(root, "local-key", "v1")

        vault = _make_fake_vault({"vault-key": "v2"})
        cascade = CascadingSecretsProvider(
            [
                ("shared", LocalSecretsProvider(root)),
                ("vault:shared", vault),
            ]
        )

        result = await cascade.list_secrets()
        assert "local-key" in result
        assert "vault-key" in result


class TestBuildCascadeWithVault:
    """Tests for build_cascade_for_user with vault_config.

    Mocks ``_create_vault_layer`` to inject fake Bitwarden providers
    so tests don't need the real SDK or access token.
    """

    @pytest.fixture()
    def secrets_root(self, tmp_path):
        root = tmp_path / "secrets"
        root.mkdir()
        _write_secret(root, "shared-token", "shared-val")
        return root

    @pytest.fixture()
    def _mock_vault_layer(self):
        """Replace _create_vault_layer with one that returns fake vaults."""
        created: list[tuple[str, str]] = []

        def _fake_create(bw_config, project_id):
            created.append((bw_config.get("organization_id", ""), project_id))
            return _make_fake_vault(
                {"vault-secret": "vault-val"},
                project_id=project_id,
                org_id=bw_config.get("organization_id", "org-1"),
            )

        with patch(
            "providers.secrets.cascade._create_vault_layer",
            side_effect=_fake_create,
        ):
            yield created

    def test_no_vault_config_unchanged(self, secrets_root):
        """Without vault_config, cascade is local-only (backward compat)."""
        cascade = build_cascade_for_user("alice", [], secrets_root)
        labels = [label for label, _ in cascade._layers]
        assert labels == ["shared"]

    def test_vault_shared_layer_added(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        """vault:shared appears after shared local when configured."""
        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
            },
        }
        cascade = build_cascade_for_user(
            "alice",
            [],
            secrets_root,
            vault_config=vault_config,
        )
        labels = [label for label, _ in cascade._layers]
        assert labels == ["shared", "vault:shared"]

    def test_vault_group_layer_ordering(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        """vault:group:<name> appears after local group layer."""
        group_dir = secrets_root / "groups" / "devs"
        group_dir.mkdir(parents=True)
        _write_secret(group_dir, "key", "group-local")

        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
                "group_project_ids": {"devs": "proj-devs"},
            },
        }
        cascade = build_cascade_for_user(
            "alice",
            ["devs"],
            secrets_root,
            vault_config=vault_config,
        )
        labels = [label for label, _ in cascade._layers]
        assert labels == [
            "group:devs",
            "vault:group:devs",
            "shared",
            "vault:shared",
        ]

    def test_vault_group_without_local_dir(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        """Vault group layer added even when local group dir is absent."""
        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
                "group_project_ids": {"devs": "proj-devs"},
            },
        }
        cascade = build_cascade_for_user(
            "alice",
            ["devs"],
            secrets_root,
            vault_config=vault_config,
        )
        labels = [label for label, _ in cascade._layers]
        assert "vault:group:devs" in labels
        assert "group:devs" not in labels

    def test_unconfigured_group_skipped(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        """Groups not in group_project_ids get no vault layer."""
        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
                "group_project_ids": {"devs": "proj-devs"},
            },
        }
        cascade = build_cascade_for_user(
            "alice",
            ["devs", "ops"],
            secrets_root,
            vault_config=vault_config,
        )
        labels = [label for label, _ in cascade._layers]
        assert "vault:group:devs" in labels
        assert "vault:group:ops" not in labels

    def test_create_vault_layer_receives_correct_project_ids(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        """Verify the correct project IDs are passed to vault creation."""
        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
                "group_project_ids": {"devs": "proj-devs"},
            },
        }
        build_cascade_for_user(
            "alice",
            ["devs"],
            secrets_root,
            vault_config=vault_config,
        )
        created_ids = [pid for _, pid in _mock_vault_layer]
        assert "proj-devs" in created_ids
        assert "proj-shared" in created_ids


class TestDispatcherVaultConfig:
    """Dispatcher passes vault_config to build_cascade_for_user."""

    @pytest.fixture()
    def secrets_root(self, tmp_path):
        root = tmp_path / "secrets"
        root.mkdir()
        _write_secret(root, "token", "shared-val")
        return root

    @pytest.fixture()
    def _mock_vault_layer(self):
        def _fake_create(bw_config, project_id):
            return _make_fake_vault(
                {},
                project_id=project_id,
                org_id=bw_config.get("organization_id", "org-1"),
            )

        with patch(
            "providers.secrets.cascade._create_vault_layer",
            side_effect=_fake_create,
        ):
            yield

    def test_vault_config_forwarded(
        self,
        secrets_root,
        _mock_vault_layer,
    ):
        from orchestrator.dispatcher import Dispatcher
        from state_store.identity import UserStore

        user_store = UserStore(persist_path=secrets_root / "users.json")
        user_store.create_user("alice")

        vault_config = {
            "bitwarden": {
                "organization_id": "org-1",
                "shared_project_id": "proj-shared",
            },
        }
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=LocalSecretsProvider(secrets_root),
            user_store=user_store,
            secrets_root=secrets_root,
            vault_config=vault_config,
        )
        cascade = dispatcher._get_secrets_for_ticket(
            {"created_by": "alice"},
        )
        assert isinstance(cascade, CascadingSecretsProvider)
        labels = [label for label, _ in cascade._layers]
        assert "vault:shared" in labels

    def test_no_vault_config_backward_compat(self, secrets_root):
        from orchestrator.dispatcher import Dispatcher
        from state_store.identity import UserStore

        user_store = UserStore(persist_path=secrets_root / "users.json")
        user_store.create_user("alice")

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            secrets_provider=LocalSecretsProvider(secrets_root),
            user_store=user_store,
            secrets_root=secrets_root,
        )
        cascade = dispatcher._get_secrets_for_ticket(
            {"created_by": "alice"},
        )
        assert isinstance(cascade, CascadingSecretsProvider)
        labels = [label for label, _ in cascade._layers]
        assert "vault:shared" not in labels
