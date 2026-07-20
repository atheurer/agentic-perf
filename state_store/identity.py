"""User and group identity store for multi-user mode.

Manages user accounts, bearer tokens (stored as SHA-256 hashes),
group membership, and admin privileges.  Persists to a JSON file
that survives state-store restarts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from paths import AGENTIC_PERF_HOME

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
DEFAULT_USERS_PATH = AGENTIC_PERF_HOME / "users.json"


class User(BaseModel):
    username: str
    token_hash: str
    is_admin: bool = False
    groups: list[str] = Field(default_factory=list)
    disabled: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class Group(BaseModel):
    name: str
    description: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


def validate_username(username: str) -> str:
    """Validate and normalize a username.  Returns lowercased form."""
    normalized = username.lower()
    if not _USERNAME_RE.match(normalized):
        raise ValueError(
            f"Invalid username '{username}': must match [a-z0-9_-]{{1,32}}"
        )
    return normalized


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class UserNotFound(Exception):
    pass


class GroupNotFound(Exception):
    pass


class DuplicateUser(Exception):
    pass


class DuplicateGroup(Exception):
    pass


class UserStore:
    """Thread-safe identity store backed by a JSON file."""

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._path = Path(persist_path) if persist_path else DEFAULT_USERS_PATH
        self._users: dict[str, User] = {}
        self._groups: dict[str, Group] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        is_admin: bool = False,
    ) -> tuple[User, str]:
        """Create a user and return (user, raw_token).

        The raw token is returned exactly once — store it immediately.
        """
        normalized = validate_username(username)
        raw_token = secrets.token_hex(32)
        token_h = hash_token(raw_token)
        with self._lock:
            if normalized in self._users:
                raise DuplicateUser(f"User '{normalized}' already exists")
            user = User(
                username=normalized,
                token_hash=token_h,
                is_admin=is_admin,
            )
            self._users[normalized] = user
            self._save()
        return user, raw_token

    def get_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            user = self._users.get(normalized)
        if user is None:
            raise UserNotFound(f"User '{username}' not found")
        return user.model_copy()

    def list_users(self) -> list[User]:
        with self._lock:
            return [u.model_copy() for u in self._users.values()]

    def disable_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            user.disabled = True
            self._save()
            return user.model_copy()

    def enable_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            user.disabled = False
            self._save()
            return user.model_copy()

    def set_admin(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            user.is_admin = True
            self._save()
            return user.model_copy()

    def remove_admin(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            user.is_admin = False
            self._save()
            return user.model_copy()

    def rotate_token(self, username: str) -> str:
        """Generate a new token for a user.  Returns the raw token once."""
        normalized = username.lower()
        raw_token = secrets.token_hex(32)
        token_h = hash_token(raw_token)
        with self._lock:
            user = self._users.get(normalized)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            user.token_hash = token_h
            self._save()
        return raw_token

    def lookup_by_token_hash(self, token_h: str) -> User | None:
        """Find a user by their token hash.  Returns None if not found."""
        with self._lock:
            for user in self._users.values():
                if user.token_hash == token_h:
                    return user.model_copy()
        return None

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    def create_group(self, name: str, description: str = "") -> Group:
        normalized = name.lower()
        if not _USERNAME_RE.match(normalized):
            raise ValueError(
                f"Invalid group name '{name}': must match [a-z0-9_-]{{1,32}}"
            )
        with self._lock:
            if normalized in self._groups:
                raise DuplicateGroup(f"Group '{normalized}' already exists")
            group = Group(name=normalized, description=description)
            self._groups[normalized] = group
            self._save()
        return group

    def delete_group(self, name: str) -> None:
        normalized = name.lower()
        with self._lock:
            if normalized not in self._groups:
                raise GroupNotFound(f"Group '{name}' not found")
            del self._groups[normalized]
            for user in self._users.values():
                if normalized in user.groups:
                    user.groups.remove(normalized)
            self._save()

    def get_group(self, name: str) -> Group:
        normalized = name.lower()
        with self._lock:
            group = self._groups.get(normalized)
        if group is None:
            raise GroupNotFound(f"Group '{name}' not found")
        return group.model_copy()

    def list_groups(self) -> list[Group]:
        with self._lock:
            return [g.model_copy() for g in self._groups.values()]

    def add_member(self, group_name: str, username: str) -> None:
        g_norm = group_name.lower()
        u_norm = username.lower()
        with self._lock:
            if g_norm not in self._groups:
                raise GroupNotFound(f"Group '{group_name}' not found")
            user = self._users.get(u_norm)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            if g_norm not in user.groups:
                user.groups.append(g_norm)
                self._save()

    def remove_member(self, group_name: str, username: str) -> None:
        g_norm = group_name.lower()
        u_norm = username.lower()
        with self._lock:
            if g_norm not in self._groups:
                raise GroupNotFound(f"Group '{group_name}' not found")
            user = self._users.get(u_norm)
            if user is None:
                raise UserNotFound(f"User '{username}' not found")
            if g_norm in user.groups:
                user.groups.remove(g_norm)
                self._save()

    def get_group_members(self, group_name: str) -> list[str]:
        g_norm = group_name.lower()
        with self._lock:
            if g_norm not in self._groups:
                raise GroupNotFound(f"Group '{group_name}' not found")
            return [u.username for u in self._users.values() if g_norm in u.groups]

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def to_safe_dict(user: User) -> dict:
        """Return user data without the token hash."""
        data = user.model_dump(mode="json")
        data.pop("token_hash", None)
        return data

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Atomic write: temp file + os.replace()."""
        data = {
            "users": {
                name: user.model_dump(mode="json") for name, user in self._users.items()
            },
            "groups": {
                name: group.model_dump(mode="json")
                for name, group in self._groups.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent),
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load users file %s: %s", self._path, exc)
            return
        for name, udata in raw.get("users", {}).items():
            try:
                self._users[name] = User.model_validate(udata)
            except Exception as exc:
                logger.warning("Skipping invalid user '%s': %s", name, exc)
        for name, gdata in raw.get("groups", {}).items():
            try:
                self._groups[name] = Group.model_validate(gdata)
            except Exception as exc:
                logger.warning("Skipping invalid group '%s': %s", name, exc)
        logger.info(
            "Loaded %d users and %d groups from %s",
            len(self._users),
            len(self._groups),
            self._path,
        )
