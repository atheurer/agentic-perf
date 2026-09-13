"""Exclusive lifetime lock and identity for a state-store persistence root."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from paths import STATE_STORE_ID_PATH, STATE_STORE_LOCK_PATH

_MAX_METADATA_BYTES = 4096


class PersistenceRootLockedError(RuntimeError):
    """Raised when another state-store process owns the persistence root."""


def _process_start_identity(pid: int | None = None) -> str:
    """Best-effort process incarnation identity, robust against PID reuse."""
    pid = pid or os.getpid()
    try:
        # Linux /proc stat field 22 is the kernel start-time tick count.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return f"{pid}:{fields[19]}"
    except (IndexError, OSError):
        return str(pid)


def _read_metadata(
    fd: int | None = None, path: Path | None = None
) -> dict[str, object]:
    try:
        if fd is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, _MAX_METADATA_BYTES)
        else:
            raw = (path or STATE_STORE_LOCK_PATH).read_bytes()[:_MAX_METADATA_BYTES]
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def ensure_store_id(path: Path = STATE_STORE_ID_PATH) -> str:
    """Return the persistent store UUID, recovering atomically after interruption.

    The caller must already hold the persistence-root lock.  Replacing instead
    of editing the identity file means a crash can leave either the old complete
    UUID or a complete new UUID, never a partially written identity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value and str(uuid.UUID(value)) == value:
            return value
    except (OSError, ValueError):
        pass
    value = str(uuid.uuid4())
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (value + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return value


@dataclass
class PersistenceRootLock:
    root: Path
    port: int
    fd: int | None = None
    session_id: str | None = None
    store_id: str | None = None

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_start_identity": _process_start_identity(),
            "configured_port": self.port,
            "startup_timestamp": time.time(),
            "session_id": self.session_id,
            "store_id": self.store_id,
        }

    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / STATE_STORE_LOCK_PATH.name
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _read_metadata(path=path)
            os.close(fd)
            detail = json.dumps(holder, sort_keys=True) if holder else "unavailable"
            raise PersistenceRootLockedError(
                f"state-store persistence root is locked: {self.root} (holder metadata: {detail})"
            ) from exc
        self.fd = fd
        try:
            self.session_id = str(uuid.uuid4())
            self.store_id = ensure_store_id(self.root / STATE_STORE_ID_PATH.name)
            encoded = json.dumps(self.metadata, sort_keys=True).encode("utf-8")
            if len(encoded) > _MAX_METADATA_BYTES:
                raise RuntimeError("state-store lock metadata exceeds bounded size")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, encoded)
            os.fsync(fd)
        except Exception:
            self.release()
            self.session_id = None
            self.store_id = None
            raise

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def close_inherited(self) -> None:
        """Discard a descriptor inherited across fork without unlocking it.

        ``flock`` ownership is attached to the inherited open-file description.
        Unlocking it in a child would also release the parent's process lock.
        Closing only the child's descriptor leaves the parent's lock intact.
        """
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.session_id = None
        self.store_id = None

    def holder_metadata(self) -> dict[str, object]:
        return _read_metadata(fd=self.fd, path=self.root / STATE_STORE_LOCK_PATH.name)
