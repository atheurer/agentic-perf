"""Tests for the cascading secrets provider and per-ticket resolution."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from providers.secrets.cascade import (
    CascadingSecretsProvider,
    build_cascade_for_user,
)
from providers.secrets.local import LocalSecretsProvider


def _write_secret(base, path, content="secret-value"):
    full = base / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


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
