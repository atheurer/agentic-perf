"""Crash-safe, per-process spool files for trace delivery.

Frames are ``length | JSON | sha256(JSON)``.  The acknowledgement is an
offset, written only after the server has acknowledged a frame; therefore a
crash can cause a replay but cannot discard an accepted event.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from paths import TRACE_SPOOL_DIR

from .models import TraceEventV1

_HEADER = struct.Struct("!I")
_DIGEST_SIZE = 32
_MAX_FRAME = 1_048_576
logger = logging.getLogger(__name__)


class SpoolError(RuntimeError):
    """A local spool cannot be safely used."""


class SpoolBackpressure(SpoolError):
    """The durable spool has reached its configured cap."""


class SpoolCorruption(SpoolError):
    """A malformed frame was found and quarantined."""


class TraceSpool:
    """An append-only durable spool owned by exactly one producer process."""

    def __init__(
        self,
        directory: Path = TRACE_SPOOL_DIR,
        *,
        name: str | None = None,
        max_bytes: int = 64 * 1024 * 1024,
        create: bool = True,
    ) -> None:
        if max_bytes < _HEADER.size + _DIGEST_SIZE:
            raise ValueError("max_bytes is too small for a trace frame")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise SpoolError("trace spool directory must not be a symlink")
        os.chmod(self.directory, 0o700)
        identity = name or f"{socket.gethostname()}-{os.getpid()}-{time.time_ns()}"
        if "/" in identity or identity in {"", ".", ".."}:
            raise ValueError("invalid spool name")
        self.path = self.directory / f"{identity}.spool"
        self.ack_path = self.directory / f"{identity}.ack"
        self.lock_path = self.directory / f"{identity}.lock"
        self.max_bytes = max_bytes
        created = not self.path.exists()
        if created and not create:
            raise FileNotFoundError(self.path)
        lock_fd = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise SpoolError("trace spool lock must be a regular file")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lock_fd)
            raise
        self._lock_fd = lock_fd
        try:
            if not create and not self.path.exists():
                self.lock_path.unlink(missing_ok=True)
                raise FileNotFoundError(self.path)
            if created:
                flags = (
                    os.O_CREAT
                    | os.O_APPEND
                    | os.O_WRONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                fd = os.open(self.path, flags, 0o600)
                os.close(fd)
                self._fsync_dir()
            if self.path.is_symlink() or not self.path.is_file():
                raise SpoolError("trace spool must be a regular file")
            os.chmod(self.path, 0o600)
        except BaseException:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            self._lock_fd = None
            raise
        self._mutex = threading.RLock()

    def append(self, event: TraceEventV1) -> None:
        with self._mutex:
            payload = event.model_dump_json().encode("utf-8")
            if len(payload) > _MAX_FRAME:
                raise SpoolBackpressure("trace event exceeds maximum frame size")
            frame = (
                _HEADER.pack(len(payload)) + payload + hashlib.sha256(payload).digest()
            )
            if self.path.stat().st_size + len(frame) > self.max_bytes:
                raise SpoolBackpressure("trace spool size cap reached")
            with self.path.open("ab", buffering=0) as stream:
                self._write_all(stream, frame)
                stream.flush()
                os.fsync(stream.fileno())

    def _ack_offset(self) -> int:
        if self.ack_path.is_symlink():
            raise SpoolCorruption("spool acknowledgement must not be a symlink")
        try:
            value = int(self.ack_path.read_text().strip())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise SpoolCorruption("invalid spool acknowledgement") from exc
        return max(value, 0)

    def _write_ack(self, offset: int) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".ack-", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(str(offset))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.ack_path)
            self._fsync_dir()
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def pending(self) -> Iterator[tuple[int, TraceEventV1]]:
        with self._mutex, self.path.open("rb") as stream:
            offset = self._validate_ack_boundary(stream, self._ack_offset())
            stream.seek(offset)
            while True:
                header = stream.read(_HEADER.size)
                if not header:
                    return
                if len(header) != _HEADER.size:
                    self._quarantine("truncated-header")
                    raise SpoolCorruption("truncated spool frame")
                length = _HEADER.unpack(header)[0]
                if length > _MAX_FRAME:
                    self._quarantine("invalid-length")
                    raise SpoolCorruption("untrusted spool frame length")
                payload = stream.read(length)
                digest = stream.read(_DIGEST_SIZE)
                if len(payload) != length or len(digest) != _DIGEST_SIZE:
                    self._quarantine("truncated-frame")
                    raise SpoolCorruption("truncated spool frame")
                if hashlib.sha256(payload).digest() != digest:
                    self._quarantine("checksum")
                    raise SpoolCorruption("spool checksum failure")
                try:
                    event = TraceEventV1.model_validate_json(payload)
                except ValueError as exc:
                    self._quarantine("invalid-json")
                    raise SpoolCorruption("invalid spool event") from exc
                yield stream.tell(), event

    def acknowledge(self, offset: int) -> None:
        with self._mutex:
            with self.path.open("rb") as stream:
                self._validate_ack_boundary(stream, offset)
            self._write_ack(offset)

    def compact(self) -> None:
        """Atomically discard acknowledged bytes; old file remains on crash."""
        with self._mutex:
            offset = self._ack_offset()
            if not offset:
                return
            fd, tmp = tempfile.mkstemp(prefix=".spool-", dir=self.directory)
            try:
                os.fchmod(fd, 0o600)
                with self.path.open("rb") as source, os.fdopen(fd, "wb") as target:
                    self._validate_ack_boundary(source, offset)
                    source.seek(offset)
                    while block := source.read(64 * 1024):
                        self._write_all(target, block)
                    target.flush()
                    os.fsync(target.fileno())
                # Reset before replace: either crash window replays the old file or
                # uses the compacted file from offset zero; neither loses data.
                self._write_ack(0)
                os.replace(tmp, self.path)
                self._fsync_dir()
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

    def bytes_pending(self) -> int:
        return max(0, self.path.stat().st_size - self._ack_offset())

    def close(self) -> None:
        with self._mutex:
            if self._lock_fd is not None:
                if self.path.exists() and self.path.stat().st_size == 0:
                    self.path.unlink()
                    self.ack_path.unlink(missing_ok=True)
                    self.lock_path.unlink(missing_ok=True)
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None
                self._fsync_dir()

    def _quarantine(self, reason: str) -> None:
        target = self.directory / f"{self.path.name}.{reason}.{time.time_ns()}.bad"
        try:
            os.replace(self.path, target)
            self.path.touch(mode=0o600)
            self.ack_path.unlink(missing_ok=True)
            self._fsync_dir()
        except OSError:
            pass

    def _fsync_dir(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate_ack_boundary(self, stream: BinaryIO, offset: int) -> int:
        """Reject acknowledgement offsets that could skip a partial frame."""
        if offset < 0:
            raise SpoolCorruption("negative spool acknowledgement")
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if offset > size:
            self._quarantine("invalid-ack")
            raise SpoolCorruption("spool acknowledgement exceeds spool length")
        stream.seek(0)
        position = 0
        while position < offset:
            header = stream.read(_HEADER.size)
            if len(header) != _HEADER.size:
                self._quarantine("invalid-ack")
                raise SpoolCorruption("spool acknowledgement is not frame aligned")
            length = _HEADER.unpack(header)[0]
            if length > _MAX_FRAME:
                self._quarantine("invalid-ack")
                raise SpoolCorruption("spool acknowledgement crosses invalid frame")
            stream.seek(length + _DIGEST_SIZE, os.SEEK_CUR)
            position = stream.tell()
        if position != offset:
            self._quarantine("invalid-ack")
            raise SpoolCorruption("spool acknowledgement is not frame aligned")
        return offset

    @staticmethod
    def _write_all(stream: BinaryIO, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = stream.write(view)
            if written is None:
                return
            if written <= 0:
                raise OSError("short spool write")
            view = view[written:]


def drain_abandoned_spools(
    directory: Path,
    deliver: Callable[[TraceEventV1], None],
) -> int:
    """Drain regular orphan spool files; symlinks are never followed.

    This intentionally only acknowledges a frame after the supplied central
    delivery callback returns.  It is suitable for the orchestrator restart
    sweeper as well as a supervisor that reaps MCP subprocesses.
    """
    root = Path(directory)
    if not root.exists():
        return 0
    if root.is_symlink() or not root.is_dir():
        raise SpoolError("trace spool directory is unsafe")
    count = 0
    for path in root.glob("*.spool"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            spool = TraceSpool(
                root, name=path.name.removesuffix(".spool"), create=False
            )
        except (BlockingIOError, FileNotFoundError):
            continue
        try:
            for offset, event in spool.pending():
                deliver(event)
                spool.acknowledge(offset)
                count += 1
            spool.compact()
        except SpoolCorruption as exc:
            logger.warning("Quarantined corrupt trace spool %s: %s", path, exc)
            continue
        finally:
            spool.close()
    return count
