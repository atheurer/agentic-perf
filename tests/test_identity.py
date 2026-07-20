"""Unit tests for the identity store (users, groups, tokens)."""

from __future__ import annotations

import pytest

from state_store.identity import (
    DuplicateGroup,
    DuplicateUser,
    GroupNotFound,
    UserNotFound,
    UserStore,
    hash_token,
    validate_username,
)


class TestValidateUsername:
    def test_valid_names(self):
        assert validate_username("alice") == "alice"
        assert validate_username("bob-smith") == "bob-smith"
        assert validate_username("user_123") == "user_123"
        assert validate_username("a") == "a"

    def test_case_normalization(self):
        assert validate_username("Alice") == "alice"
        assert validate_username("BOB") == "bob"

    def test_invalid_names(self):
        with pytest.raises(ValueError, match="Invalid username"):
            validate_username("")
        with pytest.raises(ValueError, match="Invalid username"):
            validate_username("a" * 33)
        with pytest.raises(ValueError, match="Invalid username"):
            validate_username("alice bob")
        with pytest.raises(ValueError, match="Invalid username"):
            validate_username("alice@example")
        with pytest.raises(ValueError, match="Invalid username"):
            validate_username("Alice.Smith")


class TestUserStore:
    @pytest.fixture()
    def store(self, tmp_path):
        return UserStore(persist_path=tmp_path / "users.json")

    def test_create_user(self, store):
        user, token = store.create_user("alice")
        assert user.username == "alice"
        assert not user.is_admin
        assert not user.disabled
        assert user.groups == []
        assert len(token) == 64

    def test_create_admin_user(self, store):
        user, _token = store.create_user("admin1", is_admin=True)
        assert user.is_admin

    def test_duplicate_username_rejected(self, store):
        store.create_user("alice")
        with pytest.raises(DuplicateUser):
            store.create_user("alice")

    def test_case_insensitive_duplicate(self, store):
        store.create_user("alice")
        with pytest.raises(DuplicateUser):
            store.create_user("Alice")

    def test_invalid_username_rejected(self, store):
        with pytest.raises(ValueError, match="Invalid username"):
            store.create_user("bad name!")

    def test_get_user(self, store):
        store.create_user("alice")
        user = store.get_user("alice")
        assert user.username == "alice"

    def test_get_user_case_insensitive(self, store):
        store.create_user("alice")
        user = store.get_user("Alice")
        assert user.username == "alice"

    def test_get_user_not_found(self, store):
        with pytest.raises(UserNotFound):
            store.get_user("ghost")

    def test_list_users(self, store):
        store.create_user("alice")
        store.create_user("bob")
        users = store.list_users()
        names = {u.username for u in users}
        assert names == {"alice", "bob"}

    def test_disable_enable(self, store):
        store.create_user("alice")
        user = store.disable_user("alice")
        assert user.disabled
        user = store.enable_user("alice")
        assert not user.disabled

    def test_disable_not_found(self, store):
        with pytest.raises(UserNotFound):
            store.disable_user("ghost")

    def test_set_remove_admin(self, store):
        store.create_user("alice")
        user = store.set_admin("alice")
        assert user.is_admin
        user = store.remove_admin("alice")
        assert not user.is_admin

    def test_rotate_token(self, store):
        _user, old_token = store.create_user("alice")
        old_hash = hash_token(old_token)
        new_token = store.rotate_token("alice")
        new_hash = hash_token(new_token)
        assert old_hash != new_hash
        assert store.lookup_by_token_hash(old_hash) is None
        assert store.lookup_by_token_hash(new_hash) is not None

    def test_rotate_token_not_found(self, store):
        with pytest.raises(UserNotFound):
            store.rotate_token("ghost")

    def test_lookup_by_token_hash(self, store):
        _user, token = store.create_user("alice")
        token_h = hash_token(token)
        found = store.lookup_by_token_hash(token_h)
        assert found is not None
        assert found.username == "alice"

    def test_lookup_by_token_hash_not_found(self, store):
        assert store.lookup_by_token_hash("nonexistent") is None

    def test_to_safe_dict_strips_hash(self, store):
        user, _token = store.create_user("alice")
        safe = UserStore.to_safe_dict(user)
        assert "token_hash" not in safe
        assert safe["username"] == "alice"

    def test_persistence_survives_reload(self, tmp_path):
        path = tmp_path / "users.json"
        store1 = UserStore(persist_path=path)
        store1.create_user("alice", is_admin=True)
        store1.create_group("devs", description="dev team")
        store1.add_member("devs", "alice")

        store2 = UserStore(persist_path=path)
        user = store2.get_user("alice")
        assert user.username == "alice"
        assert user.is_admin
        assert "devs" in user.groups
        groups = store2.list_groups()
        assert len(groups) == 1
        assert groups[0].name == "devs"


class TestGroupOperations:
    @pytest.fixture()
    def store(self, tmp_path):
        return UserStore(persist_path=tmp_path / "users.json")

    def test_create_group(self, store):
        group = store.create_group("devs", description="developers")
        assert group.name == "devs"
        assert group.description == "developers"

    def test_duplicate_group_rejected(self, store):
        store.create_group("devs")
        with pytest.raises(DuplicateGroup):
            store.create_group("devs")

    def test_invalid_group_name(self, store):
        with pytest.raises(ValueError, match="Invalid group name"):
            store.create_group("bad name!")

    def test_delete_group(self, store):
        store.create_group("devs")
        store.delete_group("devs")
        with pytest.raises(GroupNotFound):
            store.get_group("devs")

    def test_delete_group_removes_membership(self, store):
        store.create_user("alice")
        store.create_group("devs")
        store.add_member("devs", "alice")
        store.delete_group("devs")
        user = store.get_user("alice")
        assert "devs" not in user.groups

    def test_delete_group_not_found(self, store):
        with pytest.raises(GroupNotFound):
            store.delete_group("ghost")

    def test_list_groups(self, store):
        store.create_group("a-team")
        store.create_group("b-team")
        groups = store.list_groups()
        names = {g.name for g in groups}
        assert names == {"a-team", "b-team"}

    def test_add_remove_member(self, store):
        store.create_user("alice")
        store.create_group("devs")
        store.add_member("devs", "alice")
        user = store.get_user("alice")
        assert "devs" in user.groups

        store.remove_member("devs", "alice")
        user = store.get_user("alice")
        assert "devs" not in user.groups

    def test_add_member_group_not_found(self, store):
        store.create_user("alice")
        with pytest.raises(GroupNotFound):
            store.add_member("ghost", "alice")

    def test_add_member_user_not_found(self, store):
        store.create_group("devs")
        with pytest.raises(UserNotFound):
            store.add_member("devs", "ghost")

    def test_add_member_idempotent(self, store):
        store.create_user("alice")
        store.create_group("devs")
        store.add_member("devs", "alice")
        store.add_member("devs", "alice")
        user = store.get_user("alice")
        assert user.groups.count("devs") == 1

    def test_get_group_members(self, store):
        store.create_user("alice")
        store.create_user("bob")
        store.create_group("devs")
        store.add_member("devs", "alice")
        store.add_member("devs", "bob")
        members = store.get_group_members("devs")
        assert set(members) == {"alice", "bob"}
