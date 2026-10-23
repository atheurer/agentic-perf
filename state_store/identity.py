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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from paths import AGENTIC_PERF_HOME

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
UNSET = object()  # sentinel for "field not provided"
DEFAULT_USERS_PATH = AGENTIC_PERF_HOME / "users.json"


try:
    from providers.quota import UserQuota
except ImportError:

    class UserQuota(BaseModel):  # type: ignore[no-redef]
        """Fallback when providers.quota is not importable."""

        max_cost_usd_24h: float = 0.0
        max_cost_usd_7d: float = 0.0
        max_tokens_24h: int = 0
        max_tokens_7d: int = 0
        enforce: bool = False


class User(BaseModel):
    username: str
    token_hash: str
    is_admin: bool = False
    groups: list[str] = Field(default_factory=list)
    disabled: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    token_issued_at: datetime | None = None
    last_used_at: datetime | None = None
    service_account: bool = False
    allowed_sources: list[str] = Field(default_factory=list)  # IP/CIDR
    max_requests_per_hour: int | None = None
    llm_quota: UserQuota | None = None


class Group(BaseModel):
    name: str
    description: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    llm_quota: UserQuota | None = None


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

    _DEBOUNCE_SECONDS: float = 300.0

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._path = Path(persist_path) if persist_path else DEFAULT_USERS_PATH
        self._users: dict[str, User] = {}
        self._groups: dict[str, Group] = {}
        self._lock = threading.Lock()
        self._last_persisted: dict[str, float] = {}
        # Sample mtime BEFORE loading so a concurrent write between
        # _load() and stat() doesn't cache new mtime with old content.
        try:
            self._file_mtime = (
                self._path.stat().st_mtime if self._path.exists() else 0.0
            )
        except OSError:
            self._file_mtime = 0.0
        self._load()

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        is_admin: bool = False,
        *,
        service_account: bool = False,
        allowed_sources: list[str] | None = None,
        max_requests_per_hour: int | None = None,
    ) -> tuple[User, str]:
        """Create a user and return (user, raw_token).

        The raw token is returned exactly once — store it immediately.
        """
        normalized = validate_username(username)
        if service_account:
            if is_admin:
                raise ValueError("Service accounts cannot be admins")
            if not allowed_sources:
                raise ValueError(
                    "Service accounts require at least one allowed_sources entry"
                )
        raw_token = secrets.token_hex(32)
        token_h = hash_token(raw_token)
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                if normalized in self._users:
                    raise DuplicateUser(f"User '{normalized}' already exists")
                user = User(
                    username=normalized,
                    token_hash=token_h,
                    is_admin=is_admin,
                    token_issued_at=datetime.now(timezone.utc),
                    service_account=service_account,
                    allowed_sources=list(allowed_sources) if allowed_sources else [],
                    max_requests_per_hour=max_requests_per_hour,
                )
                self._users[normalized] = user
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
        return user, raw_token

    def get_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            self._reload_if_stale()
            user = self._users.get(normalized)
        if user is None:
            raise UserNotFound(f"User '{username}' not found")
        return user.model_copy()

    def list_users(self) -> list[User]:
        with self._lock:
            self._reload_if_stale()
            return [u.model_copy() for u in self._users.values()]

    def update_user(
        self,
        username: str,
        *,
        allowed_sources: list[str] | None = None,
        max_requests_per_hour: int | None | object = UNSET,
    ) -> User:
        """Update mutable fields on an existing user.

        Only ``allowed_sources`` and ``max_requests_per_hour`` are
        mutable.  Omitted fields (``None`` / sentinel) are left
        unchanged.  Passing ``max_requests_per_hour=None`` explicitly
        clears the limit.

        Raises:
            UserNotFound: if the user does not exist.
            ValueError: if ``allowed_sources`` is set to an empty
                list on a service account (they must retain at
                least one source restriction).
        """
        normalized = validate_username(username)
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                if allowed_sources is not None:
                    if user.service_account and not allowed_sources:
                        raise ValueError(
                            "Service accounts must have at least one allowed_sources entry"
                        )
                if (
                    max_requests_per_hour is not UNSET
                    and max_requests_per_hour is not None
                ):
                    if (
                        not isinstance(max_requests_per_hour, int)
                        or max_requests_per_hour < 0
                    ):
                        raise ValueError(
                            "max_requests_per_hour must be a non-negative integer or null"
                        )
                # Save prior values for rollback on persistence failure.
                prior_sources = list(user.allowed_sources)
                prior_rate = user.max_requests_per_hour
                if allowed_sources is not None:
                    user.allowed_sources = list(allowed_sources)
                if max_requests_per_hour is not UNSET:
                    user.max_requests_per_hour = max_requests_per_hour  # type: ignore[assignment]
                try:
                    self._write_locked(filesystem)
                except Exception:
                    user.allowed_sources = prior_sources
                    user.max_requests_per_hour = prior_rate
                    raise
                return user.model_copy()
            finally:
                self._release_file_lock(filesystem, lock_fd)

    def disable_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                user.disabled = True
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return user.model_copy()

    def enable_user(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                user.disabled = False
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return user.model_copy()

    def set_admin(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                if user.service_account:
                    raise ValueError(
                        "Service accounts cannot be granted admin privileges"
                    )
                user.is_admin = True
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return user.model_copy()

    def remove_admin(self, username: str) -> User:
        normalized = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                user.is_admin = False
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return user.model_copy()

    def rotate_token(self, username: str) -> str:
        """Generate a new token for a user.  Returns the raw token once."""
        normalized = username.lower()
        raw_token = secrets.token_hex(32)
        token_h = hash_token(raw_token)
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                user.token_hash = token_h
                user.token_issued_at = datetime.now(timezone.utc)
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
        return raw_token

    def lookup_by_token_hash(self, token_h: str) -> User | None:
        """Find a user by their token hash.  Returns None if not found."""
        with self._lock:
            self._reload_if_stale()
            for user in self._users.values():
                if user.token_hash and secrets.compare_digest(
                    user.token_hash.encode("utf-8", errors="replace"),
                    token_h.encode("utf-8", errors="replace"),
                ):
                    return user.model_copy()
        return None

    def touch_last_used(self, username: str) -> None:
        """Update last_used_at in-memory; persist at most once per debounce interval."""
        now = datetime.now(timezone.utc)
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(username)
                if user is None:
                    return
                user.last_used_at = now
                last = self._last_persisted.get(username)
                if last is None or (time.monotonic() - last) >= self._DEBOUNCE_SECONDS:
                    try:
                        self._write_locked(filesystem)
                        self._last_persisted[username] = time.monotonic()
                    except Exception:
                        logger.warning(
                            "Failed to persist last_used_at for %s",
                            username,
                            exc_info=True,
                        )
            finally:
                self._release_file_lock(filesystem, lock_fd)

    def set_user_quota(
        self,
        username: str,
        quota: UserQuota | None,
    ) -> User:
        """Set or clear a user's LLM quota."""
        normalized = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                user = self._users.get(normalized)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                user.llm_quota = quota
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return user.model_copy()

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    def set_group_quota(
        self,
        group_name: str,
        quota: UserQuota | None,
    ) -> Group:
        """Set or clear a group's LLM quota."""
        normalized = group_name.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                group = self._groups.get(normalized)
                if group is None:
                    raise GroupNotFound(f"Group '{group_name}' not found")
                group.llm_quota = quota
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
            return group.model_copy()

    def create_group(self, name: str, description: str = "") -> Group:
        normalized = name.lower()
        if not _USERNAME_RE.match(normalized):
            raise ValueError(
                f"Invalid group name '{name}': must match [a-z0-9_-]{{1,32}}"
            )
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                if normalized in self._groups:
                    raise DuplicateGroup(f"Group '{normalized}' already exists")
                group = Group(name=normalized, description=description)
                self._groups[normalized] = group
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)
        return group

    def delete_group(self, name: str) -> None:
        normalized = name.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                if normalized not in self._groups:
                    raise GroupNotFound(f"Group '{name}' not found")
                del self._groups[normalized]
                for user in self._users.values():
                    if normalized in user.groups:
                        user.groups.remove(normalized)
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)

    def get_group(self, name: str) -> Group:
        normalized = name.lower()
        with self._lock:
            self._reload_if_stale()
            group = self._groups.get(normalized)
        if group is None:
            raise GroupNotFound(f"Group '{name}' not found")
        return group.model_copy()

    def list_groups(self) -> list[Group]:
        with self._lock:
            self._reload_if_stale()
            return [g.model_copy() for g in self._groups.values()]

    def add_member(self, group_name: str, username: str) -> None:
        g_norm = group_name.lower()
        u_norm = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                if g_norm not in self._groups:
                    raise GroupNotFound(f"Group '{group_name}' not found")
                user = self._users.get(u_norm)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                if g_norm not in user.groups:
                    user.groups.append(g_norm)
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)

    def remove_member(self, group_name: str, username: str) -> None:
        g_norm = group_name.lower()
        u_norm = username.lower()
        with self._lock:
            filesystem, lock_fd = self._acquire_file_lock()
            try:
                if g_norm not in self._groups:
                    raise GroupNotFound(f"Group '{group_name}' not found")
                user = self._users.get(u_norm)
                if user is None:
                    raise UserNotFound(f"User '{username}' not found")
                if g_norm in user.groups:
                    user.groups.remove(g_norm)
                self._write_locked(filesystem)
            finally:
                self._release_file_lock(filesystem, lock_fd)

    def get_group_members(self, group_name: str) -> list[str]:
        g_norm = group_name.lower()
        with self._lock:
            self._reload_if_stale()
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

    def _acquire_file_lock(self) -> tuple:
        """Acquire cross-process file lock and reload state.

        Returns (filesystem, lock_fd).  Caller MUST call
        _release_file_lock when done.  Must hold self._lock.

        After return, self._users/_groups reflect disk state.
        The caller mutates and calls _write_locked() to persist.
        """
        import fcntl

        from providers.execution import AuditedFilesystem

        filesystem = AuditedFilesystem.system(self._path.parent)
        filesystem.mkdir(".", mode=0o777)
        lock_path = self._path.with_suffix(".lock")
        filesystem.touch(lock_path.name, mode=0o600)
        lock_fd = filesystem.open_descriptor(lock_path.name, os.O_RDWR, mode=0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            self._reload_if_stale()
        except Exception:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                filesystem.forget_descriptor(lock_fd)
                os.close(lock_fd)
            except OSError:
                pass
            raise
        return filesystem, lock_fd

    def _release_file_lock(self, filesystem, lock_fd: int) -> None:
        """Release cross-process file lock."""
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        filesystem.forget_descriptor(lock_fd)
        os.close(lock_fd)

    def _write_locked(self, filesystem) -> None:
        """Persist current state.  Must hold the file lock."""
        data = {
            "users": {
                name: user.model_dump(mode="json") for name, user in self._users.items()
            },
            "groups": {
                name: group.model_dump(mode="json")
                for name, group in self._groups.items()
            },
        }
        filesystem.write(self._path.name, json.dumps(data, indent=2), mode=0o600)
        try:
            self._file_mtime = self._path.stat().st_mtime
        except OSError:
            pass

    def _reload_if_stale(self) -> None:
        """Re-read the JSON file if another process has modified it.

        Must be called while ``self._lock`` is held.
        """
        try:
            pre_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if pre_mtime == self._file_mtime:
            return
        logger.debug(
            "users.json changed on disk (mtime %.3f -> %.3f), reloading",
            self._file_mtime,
            pre_mtime,
        )
        self._load()
        post_mtime = self._path.stat().st_mtime
        if post_mtime != pre_mtime:
            # File changed during read — re-read once more
            self._load()
        self._file_mtime = self._path.stat().st_mtime

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load users file %s: %s", self._path, exc)
            return
        self._users.clear()
        self._groups.clear()
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
        try:
            self._file_mtime = self._path.stat().st_mtime
        except OSError:
            pass
        logger.info(
            "Loaded %d users and %d groups from %s",
            len(self._users),
            len(self._groups),
            self._path,
        )
