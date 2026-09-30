"""Exclusive lifetime lock and identity for a state-store persistence root."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Seconds to wait for a stale flock to release on network
# filesystems (Ceph RBD, NFS) where kernel flocks may not
# be released immediately when a pod is killed.
_STALE_FLOCK_TIMEOUT_SECS = 30

from paths import STATE_STORE_ID_PATH, STATE_STORE_LOCK_PATH
from providers.execution import AuditedFilesystem

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


def _k8s_pod_exists(hostname: str) -> bool | None:
    """Check whether a Kubernetes pod with the given name exists.

    Returns True if the pod exists, False if the API confirms it
    does not (404), or None if the API is unavailable (not running
    in K8s, no permissions, network error).
    """
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if not token_path.exists():
        return None
    try:
        token = token_path.read_text().strip()
        namespace = ns_path.read_text().strip()
        import ssl
        import urllib.request

        ctx = ssl.create_default_context(cafile=str(ca_path))
        url = (
            f"https://kubernetes.default.svc/api/v1"
            f"/namespaces/{namespace}/pods/{hostname}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, context=ctx, timeout=5)
        return True
    except Exception as exc:
        # 404 = pod definitively gone
        if hasattr(exc, "code") and exc.code == 404:  # type: ignore[union-attr]
            return False
        # Any other error = inconclusive
        return None


def _holder_alive(holder: dict) -> bool:
    """Check if the lock holder is still alive.

    Uses a layered approach:
    1. Same hostname: PID + incarnation identity check (kernel-level,
       valid within the same PID namespace).
    2. Different hostname with K8s API: authoritative pod existence
       check via the Kubernetes API.  A 404 proves the pod is dead.
    3. Different hostname without K8s API: fail closed (assume alive)
       because os.kill only checks the local PID namespace.
    """
    holder_hostname = holder.get("hostname", "")
    my_hostname = socket.gethostname()

    if holder_hostname and holder_hostname != my_hostname:
        # Cross-pod scenario: PID check is unreliable (different
        # PID namespaces).  Use the Kubernetes API as the
        # authoritative fence.
        k8s_alive = _k8s_pod_exists(holder_hostname)
        if k8s_alive is False:
            # K8s API confirms the pod is gone.
            return False
        # k8s_alive is True (pod exists) or None (API unavailable).
        # Either way, fail closed — do not assume the holder is dead.
        return True

    # Same hostname (or no hostname recorded): PID check is valid
    # within this PID namespace.
    pid_value = holder.get("pid")
    if pid_value is None:
        return True
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return True
    # PID exists — verify it's the same incarnation.
    holder_identity = holder.get("process_start_identity", "")
    if not holder_identity:
        return True
    current_identity = _process_start_identity(pid)
    if current_identity == str(pid) and holder_identity != str(pid):
        return True
    return current_identity == holder_identity


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
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value and str(uuid.UUID(value)) == value:
            return value
    except (OSError, ValueError):
        pass
    value = str(uuid.uuid4())
    filesystem = AuditedFilesystem.system(path.parent)
    filesystem.mkdir(".", mode=0o777)
    filesystem.write(path.name, value + "\n", mode=0o600)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return value


@dataclass
class PersistenceRootLock:
    root: Path
    port: int
    fd: int | None = None
    session_id: str | None = None
    store_id: str | None = None
    _filesystem: AuditedFilesystem | None = field(default=None, repr=False)

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
        filesystem = AuditedFilesystem.system(self.root)
        filesystem.mkdir(".")
        self._filesystem = filesystem
        path = self.root / STATE_STORE_LOCK_PATH.name
        fd = filesystem.open_descriptor(path.name, os.O_RDWR | os.O_CREAT, mode=0o600)
        try:
            filesystem.lock_descriptor(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _read_metadata(path=path)
            # If the holder process is no longer the same
            # incarnation, the lock is stale (e.g., container
            # restart with PVC).  Force-acquire by blocking
            # on the SAME inode with a timeout.
            if not _holder_alive(holder):
                logger.warning(
                    "Stale lock held by dead process %s — "
                    "waiting up to %ds for flock release",
                    holder.get(
                        "process_start_identity",
                        holder.get("pid"),
                    ),
                    _STALE_FLOCK_TIMEOUT_SECS,
                )

                # Use a blocking flock with SIGALRM timeout.
                # This avoids the unlink race: we never delete
                # the lock file, so competing starters cannot
                # acquire different inodes.  On local filesystems
                # the flock is already released (the process is
                # dead); on network filesystems (Ceph RBD) it
                # may take a few seconds for the server to
                # reclaim it.
                def _alarm_handler(signum: int, frame: object) -> None:
                    raise OSError("Timed out waiting for stale flock")

                old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
                old_alarm = signal.alarm(_STALE_FLOCK_TIMEOUT_SECS)
                try:
                    filesystem.lock_descriptor(fd, fcntl.LOCK_EX)
                except OSError:
                    # Timeout — the flock is genuinely stuck on
                    # the network filesystem.  Do NOT unlink and
                    # recreate: that creates a second inode and
                    # two processes could each lock a different
                    # one (split-brain).  Raise so the operator
                    # can intervene.
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
                    filesystem.forget_descriptor(fd)
                    os.close(fd)
                    detail = (
                        json.dumps(holder, sort_keys=True) if holder else "unavailable"
                    )
                    raise PersistenceRootLockedError(
                        f"state-store persistence root is "
                        f"locked (stale flock not released "
                        f"after {_STALE_FLOCK_TIMEOUT_SECS}s "
                        f"— manual lock file removal required): "
                        f"{self.root} "
                        f"(holder metadata: {detail})"
                    ) from exc
                finally:
                    signal.alarm(old_alarm)
                    signal.signal(signal.SIGALRM, old_handler)
            else:
                filesystem.forget_descriptor(fd)
                os.close(fd)
                detail = json.dumps(holder, sort_keys=True) if holder else "unavailable"
                raise PersistenceRootLockedError(
                    f"state-store persistence root is "
                    f"locked: {self.root} "
                    f"(holder metadata: {detail})"
                ) from exc
        self.fd = fd
        try:
            self.session_id = str(uuid.uuid4())
            self.store_id = ensure_store_id(self.root / STATE_STORE_ID_PATH.name)
            encoded = json.dumps(self.metadata, sort_keys=True).encode("utf-8")
            if len(encoded) > _MAX_METADATA_BYTES:
                raise RuntimeError("state-store lock metadata exceeds bounded size")
            filesystem.write_descriptor(
                fd, encoded, truncate=True, seek_start=True, sync=True
            )
        except Exception:
            self.release()
            self.session_id = None
            self.store_id = None
            raise

    def release(self) -> None:
        if self.fd is not None:
            if self._filesystem is not None:
                self._filesystem.lock_descriptor(self.fd, fcntl.LOCK_UN)
                self._filesystem.forget_descriptor(self.fd)
            os.close(self.fd)
            self.fd = None
        self._filesystem = None

    def close_inherited(self) -> None:
        """Discard a descriptor inherited across fork without unlocking it.

        ``flock`` ownership is attached to the inherited open-file description.
        Unlocking it in a child would also release the parent's process lock.
        Closing only the child's descriptor leaves the parent's lock intact.
        """
        if self.fd is not None:
            if self._filesystem is not None:
                self._filesystem.forget_descriptor(self.fd)
            os.close(self.fd)
            self.fd = None
        self._filesystem = None
        self.session_id = None
        self.store_id = None

    def holder_metadata(self) -> dict[str, object]:
        return _read_metadata(fd=self.fd, path=self.root / STATE_STORE_LOCK_PATH.name)
