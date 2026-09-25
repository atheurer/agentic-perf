"""Ticket-rooted filesystem mutations with durable, redacted audit records."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import os
import re
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from providers.tracing import (
        ErrorDescriptor,
        LifecycleState,
        TraceContext,
        TraceEventV1,
    )

_TARGET_LOCKS: dict[Path, threading.Lock] = {}
_TARGET_LOCKS_GUARD = threading.Lock()
_SPOOL_EMITTER_LOCK = threading.Lock()
_SPOOL_EMITTER: Callable[[TraceEventV1], None] | None = None


def durable_filesystem_emitter() -> Callable[[TraceEventV1], None]:
    """Return the process-managed durable fallback for ticket mutations.

    The state-store trace recorder remains preferred.  MCP/offline paths do not
    always own one, so they append to the existing crash-safe trace spool; the
    normal startup sweep exports those frames.  This is intentionally not a
    best-effort logger: failure to create or append the spool fails the action.
    """
    global _SPOOL_EMITTER
    with _SPOOL_EMITTER_LOCK:
        if _SPOOL_EMITTER is None:
            from providers.tracing import TraceSpool

            spool = TraceSpool(name=f"filesystem-{os.getpid()}")
            _SPOOL_EMITTER = spool.append
        return _SPOOL_EMITTER


class FilesystemAuditError(RuntimeError):
    """Raised when a required audit record cannot be durably written."""


class RootedPath:
    """A physical root paired with the only path representation traces expose."""

    def __init__(
        self,
        root: Path | str,
        scheme: str,
        *,
        physical_prefix: str = "",
        logical_prefix: str = "",
    ) -> None:
        self.root = Path(root).resolve()
        self.scheme = scheme.rstrip(":/")
        self.physical_prefix = physical_prefix.strip("/")
        self.logical_prefix = logical_prefix.strip("/")

    def resolve(
        self, relative: str | Path, *, follow_final: bool = False
    ) -> tuple[Path, str]:
        """Resolve contained parents while preserving the final directory entry.

        Unlink, rename, and atomic replacement operate on the final link entry.
        Callers that follow a final symlink opt in and still reject targets
        outside the root.
        """
        raw = Path(str(relative).removeprefix(f"{self.scheme}://"))
        candidate = raw if raw.is_absolute() else self.root / raw
        candidate = Path(os.path.normpath(str(candidate)))
        if candidate == self.root:
            entry = self.root
        else:
            parent = candidate.parent.resolve()
            try:
                parent.relative_to(self.root)
            except ValueError as exc:
                raise ValueError("filesystem target escapes its audited root") from exc
            entry = parent / candidate.name
        target = entry.resolve() if follow_final else entry
        try:
            clean = entry.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("filesystem target escapes its audited root") from exc
        if follow_final:
            try:
                target.relative_to(self.root)
            except ValueError as exc:
                raise ValueError("filesystem target escapes its audited root") from exc
        parts = clean.parts
        prefix = Path(self.physical_prefix).parts
        if prefix and parts[: len(prefix)] == prefix:
            parts = parts[len(prefix) :]
        # A caller-controlled filename can itself contain a credential.  Keep
        # normal owner-visible names, but replace credential-looking components
        # with a deterministic opaque reference before they enter any event.
        safe_parts = [
            (
                f"redacted-{hashlib.sha256(part.encode()).hexdigest()[:16]}"
                if re.search(
                    r"(?:secret|token|password|credential|private.?key|id_rsa|id_ed25519|\.pem$|\.key$|\.env$)",
                    part,
                    re.I,
                )
                else part
            )
            for part in parts
        ]
        rendered = "/".join((*Path(self.logical_prefix).parts, *safe_parts))
        return target, f"{self.scheme}://{rendered}"


class AuditedStream:
    """A write handle whose filesystem action completes only on close."""

    def __init__(
        self,
        filesystem,
        handle,
        path,
        target,
        context,
        started,
        attributes,
        *,
        sensitive: bool = False,
    ):
        self._filesystem, self._handle, self._path = filesystem, handle, path
        self._target, self._context, self._started = target, context, started
        self._attributes, self._closed = attributes, False
        self._sensitive = sensitive

    def __getattr__(self, name: str):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._filesystem._system_context:
            try:
                self._handle.flush()
                os.fsync(self._handle.fileno())
            finally:
                self._handle.close()
            return
        from providers.tracing import LifecycleState

        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            descriptor = self._filesystem._path_descriptor(
                self._path,
                sensitive=self._sensitive,
            )
            self._handle.close()
            self._filesystem._record(
                self._filesystem._event(
                    self._context,
                    LifecycleState.COMPLETED,
                    "stream_write",
                    self._target,
                    self._started,
                    terminal=True,
                    attributes=self._attributes | descriptor,
                )
            )
        except Exception as exc:
            try:
                self._handle.close()
            except OSError:
                pass
            error, digest = self._filesystem._error(exc)
            self._filesystem._record(
                self._filesystem._event(
                    self._context,
                    LifecycleState.FAILED,
                    "stream_write",
                    self._target,
                    self._started,
                    terminal=True,
                    attributes=self._attributes | {"error_digest": digest},
                    error=error,
                )
            )
            raise


class AuditedFilesystem:
    """Mutate a rooted namespace without exposing physical details in traces.

    Critical ticket operations require an emitter; requested is persisted before
    the operation.  ``system_context`` is only for explicitly non-ticket
    bootstrap/scratch operations and can never be combined with ``critical``.
    """

    def __init__(
        self,
        root: RootedPath,
        *,
        ticket_id: str,
        emit: Callable[[TraceEventV1], Any] | None = None,
        critical: bool = False,
        system_context: bool = False,
    ) -> None:
        if critical and emit is None:
            raise FilesystemAuditError(
                "critical filesystem mutations require an audit recorder"
            )
        if critical and system_context:
            raise ValueError("critical filesystem mutations cannot use system context")
        if emit is None and not system_context:
            raise FilesystemAuditError(
                "filesystem mutations without an audit recorder require system_context"
            )
        self.root, self.ticket_id, self._emit = root, ticket_id, emit
        self._critical = critical
        self._system_context = system_context
        self._descriptor_targets: dict[int, str] = {}

    @classmethod
    def system(cls, root: str | Path, *, scheme: str = "system") -> AuditedFilesystem:
        """Create a deliberately unaudited wrapper for non-ticket system state."""
        return cls(
            RootedPath(root, scheme),
            ticket_id="system",
            system_context=True,
        )

    @staticmethod
    def _descriptor(data: bytes) -> dict[str, Any]:
        return {
            "size_bytes": len(data),
            "digest": hashlib.sha256(data).hexdigest(),
            "digest_kind": "sha256",
        }

    @staticmethod
    def _path_descriptor(path: Path, *, sensitive: bool = False) -> dict[str, Any]:
        size = 0
        digest = None if sensitive else hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if digest is not None:
                    digest.update(chunk)
        if digest is None:
            return {"size_bytes": size, "sensitive": True}
        return {
            "size_bytes": size,
            "digest": digest.hexdigest(),
            "digest_kind": "sha256",
        }

    @staticmethod
    def _sensitive_name(relative: str | Path) -> bool:
        return any(
            re.search(
                r"(?:secret|token|password|credential|private.?key|id_rsa|id_ed25519|\.pem$|\.key$|\.env$)",
                part,
                re.I,
            )
            for part in Path(str(relative)).parts
        )

    @staticmethod
    def _error(exc: BaseException) -> tuple[ErrorDescriptor, str]:
        from providers.tracing import ErrorDescriptor

        digest = hashlib.sha256(
            f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}".encode()
        ).hexdigest()
        return ErrorDescriptor(
            type=type(exc).__name__[:80],
            code=str(getattr(exc, "errno", "filesystem_error"))[:40],
            retryable=False,
        ), digest

    @staticmethod
    def _target_lock(path: Path) -> threading.Lock:
        """Serialize same-process writes so post-replace verification is meaningful."""
        with _TARGET_LOCKS_GUARD:
            return _TARGET_LOCKS.setdefault(path, threading.Lock())

    def _record(self, event: TraceEventV1) -> None:
        if self._emit is None:
            if self._critical:
                raise FilesystemAuditError(
                    "critical filesystem audit recorder is unavailable"
                )
            return
        try:
            if inspect.isawaitable(self._emit(event)):
                raise FilesystemAuditError(
                    "filesystem audit emitter must be synchronous"
                )
        except Exception as exc:
            raise FilesystemAuditError("filesystem audit delivery failed") from exc

    def _event(
        self,
        context: TraceContext,
        state: LifecycleState,
        operation: str,
        target: str,
        started: float,
        *,
        terminal: bool = False,
        attributes: dict[str, Any] | None = None,
        error: ErrorDescriptor | None = None,
    ) -> TraceEventV1:
        from providers.tracing import (
            ActionDescriptor,
            ActionType,
            LifecycleDescriptor,
            LifecycleState,
            OperationOutcome,
            TraceEventV1,
        )

        return TraceEventV1(
            ticket_id=self.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            action=ActionDescriptor(type=ActionType.FILESYSTEM, target=target),
            lifecycle=LifecycleDescriptor(state=state),
            duration_ms=(time.monotonic() - started) * 1000 if terminal else None,
            outcome=(
                OperationOutcome.SUCCESS
                if state == LifecycleState.COMPLETED
                else OperationOutcome.FAILURE
            )
            if terminal
            else None,
            error=error,
            attributes={"operation": operation, "atomic": False, **(attributes or {})},
        )

    def _cleanup_failure(self, target: str, exc: BaseException) -> None:
        # Cleanup is an independent operation: its failure must not replace the
        # original write/archive failure or disclose temporary physical paths.
        if self._system_context:
            return
        from providers.tracing import (
            LifecycleState,
            child_context,
            current_trace_context,
            new_trace_context,
        )

        parent = current_trace_context() or new_trace_context(
            ticket_id=self.ticket_id, agent_id="system"
        )
        context = child_context(parent)
        error, digest = self._error(exc)
        self._record(
            self._event(
                context,
                LifecycleState.FAILED,
                "cleanup",
                target,
                time.monotonic(),
                terminal=True,
                attributes={"error_digest": digest},
                error=error,
            )
        )

    def _mutate(
        self,
        operation: str,
        target: str,
        action: Callable[[], Any],
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        if self._system_context:
            return action()
        from providers.tracing import (
            LifecycleState,
            child_context,
            current_trace_context,
            new_trace_context,
        )

        started = time.monotonic()
        parent = current_trace_context()
        context = (
            child_context(parent)
            if parent
            else new_trace_context(ticket_id=self.ticket_id, agent_id="system")
        )
        # Keep this one context for exactly one requested and one terminal event.
        self._record(
            self._event(
                context,
                LifecycleState.REQUESTED,
                operation,
                target,
                started,
                attributes=attributes,
            )
        )
        try:
            result = action()
        except Exception as exc:
            error, digest = self._error(exc)
            self._record(
                self._event(
                    context,
                    LifecycleState.FAILED,
                    operation,
                    target,
                    started,
                    terminal=True,
                    attributes={**(attributes or {}), "error_digest": digest},
                    error=error,
                )
            )
            raise
        self._record(
            self._event(
                context,
                LifecycleState.COMPLETED,
                operation,
                target,
                started,
                terminal=True,
                attributes=attributes,
            )
        )
        return result

    def mkdir(
        self,
        relative: str | Path,
        *,
        mode: int = 0o700,
        parents: bool = True,
        exist_ok: bool = True,
    ) -> Path:
        path, logical = self.root.resolve(relative)
        return self._mutate(
            "mkdir",
            logical,
            lambda: (
                path.mkdir(parents=parents, exist_ok=exist_ok, mode=mode),
                path,
            )[1],
            attributes={"mode": oct(mode), "parents": parents, "exist_ok": exist_ok},
        )

    def rmdir(self, relative: str | Path) -> None:
        path, logical = self.root.resolve(relative)
        return self._mutate("rmdir", logical, path.rmdir)

    def touch(self, relative: str | Path, *, mode: int = 0o600) -> Path:
        path, logical = self.root.resolve(relative, follow_final=True)
        return self._mutate(
            "touch",
            logical,
            lambda: (path.touch(mode=mode), path)[1],
            attributes={"mode": oct(mode)},
        )

    def chmod(self, relative: str | Path, mode: int) -> None:
        path, logical = self.root.resolve(relative, follow_final=True)
        return self._mutate(
            "chmod",
            logical,
            lambda: os.chmod(path, mode),
            attributes={"mode": oct(mode)},
        )

    def temporary_directory(
        self,
        relative_parent: str | Path = ".",
        *,
        prefix: str = "tmp-",
    ) -> Path:
        parent, logical_parent = self.root.resolve(relative_parent, follow_final=True)

        def create() -> Path:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))

        return self._mutate(
            "temporary_directory",
            logical_parent,
            create,
        )

    def temporary_file(
        self,
        relative_parent: str | Path = ".",
        *,
        prefix: str = "tmp-",
        suffix: str = "",
        mode: int = 0o600,
    ) -> Path:
        parent, logical_parent = self.root.resolve(relative_parent, follow_final=True)

        def create() -> Path:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=parent)
            try:
                os.fchmod(fd, mode)
            except BaseException:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)
            return Path(name)

        return self._mutate(
            "temporary_file", logical_parent, create, attributes={"mode": oct(mode)}
        )

    def open_descriptor(
        self,
        relative: str | Path,
        flags: int,
        *,
        mode: int = 0o600,
    ) -> int:
        path, logical = self.root.resolve(relative)

        def open_file() -> int:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow and flags & nofollow:
                return os.open(path, flags, mode)
            followed = path.resolve()
            try:
                followed.relative_to(self.root.root)
            except ValueError as exc:
                raise ValueError("filesystem target escapes its audited root") from exc
            return os.open(followed, flags, mode)

        fd = self._mutate(
            "open_descriptor",
            logical,
            open_file,
            attributes={"mode": oct(mode)},
        )
        self._descriptor_targets[fd] = logical
        return fd

    def write_descriptor(
        self,
        fd: int,
        data: bytes,
        *,
        truncate: bool = False,
        seek_start: bool = False,
        sync: bool = False,
    ) -> None:
        logical = self._descriptor_targets.get(fd, "descriptor://unknown")

        def write_data() -> None:
            if truncate:
                os.ftruncate(fd, 0)
            if seek_start:
                os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short filesystem write")
                view = view[written:]
            if sync:
                os.fsync(fd)

        self._mutate("descriptor_write", logical, write_data)

    def set_descriptor_mode(self, fd: int, mode: int) -> None:
        logical = self._descriptor_targets.get(fd, "descriptor://unknown")
        self._mutate(
            "descriptor_chmod",
            logical,
            lambda: os.fchmod(fd, mode),
            attributes={"mode": oct(mode)},
        )

    def forget_descriptor(self, fd: int) -> None:
        self._descriptor_targets.pop(fd, None)

    def lock_descriptor(self, fd: int, operation: int) -> None:
        logical = self._descriptor_targets.get(fd, "descriptor://unknown")
        self._mutate("descriptor_lock", logical, lambda: fcntl.flock(fd, operation))

    def hardlink(self, source: str | Path, destination: str | Path) -> Path:
        source_path, source_logical = self.root.resolve(source, follow_final=True)
        destination_path, destination_logical = self.root.resolve(destination)
        return self._mutate(
            "hardlink",
            source_logical,
            lambda: (os.link(source_path, destination_path), destination_path)[1],
            attributes={"destination": destination_logical},
        )

    def append(
        self,
        relative: str | Path,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
        sync: bool = False,
    ) -> Path:
        path, logical = self.root.resolve(relative, follow_final=True)
        data = content if isinstance(content, bytes) else content.encode(encoding)

        def append_data() -> Path:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("ab", buffering=0) as stream:
                view = memoryview(data)
                while view:
                    written = stream.write(view)
                    if written is None or written <= 0:
                        raise OSError("short filesystem append")
                    view = view[written:]
                if sync:
                    os.fsync(stream.fileno())
            return path

        return self._mutate(
            "append",
            logical,
            append_data,
            attributes={"size_bytes": len(data)},
        )

    def write_stream(
        self,
        relative: str | Path,
        source: BinaryIO,
        *,
        mode: int = 0o600,
        chunk_bytes: int = 64 * 1024,
    ) -> Path:
        """Copy a binary input stream to a file and durably flush it."""
        path, logical = self.root.resolve(relative, follow_final=True)

        def write_data() -> Path:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(path, "wb") as destination:
                os.chmod(path, mode)
                while block := source.read(chunk_bytes):
                    view = memoryview(block)
                    while view:
                        written = destination.write(view)
                        if written is None or written <= 0:
                            raise OSError("short filesystem stream write")
                        view = view[written:]
                destination.flush()
                os.fsync(destination.fileno())
            return path

        return self._mutate(
            "stream_copy", logical, write_data, attributes={"mode": oct(mode)}
        )

    def open_stream(self, relative: str | Path, *, mode: int = 0o600) -> AuditedStream:
        """Open an audited output stream; callers must close it to finalize."""
        path, logical = self.root.resolve(relative, follow_final=True)
        sensitive = self._sensitive_name(relative) or self._sensitive_name(
            path.relative_to(self.root.root)
        )
        attributes = {"mode": oct(mode), "atomic": False, "stream": True}
        if sensitive:
            attributes["sensitive"] = True
        if self._system_context:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = open(path, "wb")
            try:
                os.chmod(path, mode)
            except Exception:
                handle.close()
                raise
            return AuditedStream(
                self,
                handle,
                path,
                logical,
                None,
                time.monotonic(),
                attributes,
                sensitive=sensitive,
            )

        from providers.tracing import (
            LifecycleState,
            child_context,
            current_trace_context,
            new_trace_context,
        )

        started = time.monotonic()
        parent = current_trace_context()
        context = (
            child_context(parent)
            if parent
            else new_trace_context(ticket_id=self.ticket_id, agent_id="system")
        )
        self._record(
            self._event(
                context,
                LifecycleState.REQUESTED,
                "stream_write",
                logical,
                started,
                attributes=attributes,
            )
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = open(path, "wb")
            os.chmod(path, mode)
            return AuditedStream(
                self,
                handle,
                path,
                logical,
                context,
                started,
                attributes,
                sensitive=sensitive,
            )
        except Exception as exc:
            error, digest = self._error(exc)
            self._record(
                self._event(
                    context,
                    LifecycleState.FAILED,
                    "stream_write",
                    logical,
                    started,
                    terminal=True,
                    attributes=attributes | {"error_digest": digest},
                    error=error,
                )
            )
            raise

    def write(
        self,
        relative: str | Path,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
        mode: int | None = 0o600,
        atomic: bool = True,
    ) -> Path:
        path, logical = self.root.resolve(relative, follow_final=not atomic)
        data = content if isinstance(content, bytes) else content.encode(encoding)
        operation = "replace" if path.exists() or path.is_symlink() else "create"
        sensitive = self._sensitive_name(relative) or self._sensitive_name(
            path.relative_to(self.root.root)
        )
        descriptor = (
            {"size_bytes": len(data), "sensitive": True}
            if sensitive
            else self._descriptor(data)
        )
        attributes = descriptor | {
            "atomic": atomic,
            "write_kind": operation,
        }
        if mode is not None:
            attributes["mode"] = oct(mode)

        def write_file() -> Path:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary: str | None = None
            try:
                if atomic:
                    fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=path.parent)
                    handle = os.fdopen(fd, "wb")
                else:
                    handle = open(path, "wb")
                with handle:
                    if mode is not None:
                        os.chmod(temporary or path, mode)
                    if handle.write(data) != len(data):
                        raise OSError("short filesystem write")
                    handle.flush()
                    os.fsync(handle.fileno())
                if temporary:
                    os.replace(temporary, path)
                    temporary = None
                if self._path_descriptor(path) != self._descriptor(data):
                    raise OSError("filesystem write verification failed")
                return path
            except Exception as primary:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError as cleanup:
                        try:
                            self._cleanup_failure(logical, cleanup)
                        except FilesystemAuditError as audit_error:
                            primary.add_note(
                                "cleanup audit delivery failed: "
                                f"{type(audit_error).__name__}"
                            )
                        primary.add_note("temporary filesystem cleanup failed")
                raise

        # Atomic replacement protects readers; this lock additionally ensures
        # one writer cannot make another writer's post-write digest check fail.
        with self._target_lock(path):
            return self._mutate(operation, logical, write_file, attributes=attributes)

    def rename(self, source: str | Path, destination: str | Path) -> Path:
        source_path, source_logical = self.root.resolve(source)
        destination_path, destination_logical = self.root.resolve(destination)
        return self._mutate(
            "rename",
            source_logical,
            lambda: (os.replace(source_path, destination_path), destination_path)[1],
            attributes={"destination": destination_logical, "atomic": True},
        )

    def unlink(self, relative: str | Path, *, missing_ok: bool = False) -> None:
        path, logical = self.root.resolve(relative)
        return self._mutate(
            "unlink",
            logical,
            lambda: path.unlink(missing_ok=missing_ok),
            attributes={"missing_ok": missing_ok},
        )

    def archive(self, destination: str | Path, members: Iterable[str | Path]) -> Path:
        target, logical = self.root.resolve(destination)
        resolved = []
        sensitive = self._sensitive_name(destination) or self._sensitive_name(
            target.relative_to(self.root.root)
        )
        for member in members:
            member_path, member_logical = self.root.resolve(member, follow_final=True)
            resolved.append((member_path, member_logical))
            sensitive = (
                sensitive
                or self._sensitive_name(member)
                or self._sensitive_name(member_path.relative_to(self.root.root))
            )
        attributes = {
            "members": [reference for _, reference in resolved],
            "atomic": True,
        }
        if sensitive:
            attributes["sensitive"] = True

        def create_archive() -> Path:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(
                prefix=".audit-", suffix=".tar.gz", dir=target.parent
            )
            os.close(fd)
            try:
                with tarfile.open(temporary, "w:gz") as archive:
                    for path, reference in resolved:
                        archive.add(path, arcname=reference.split("://", 1)[1])
                os.replace(temporary, target)
                if not target.is_file() or not target.stat().st_size:
                    raise OSError("archive verification failed")
                attributes.update(self._path_descriptor(target, sensitive=sensitive))
                return target
            except Exception as primary:
                try:
                    os.unlink(temporary)
                except OSError as cleanup:
                    try:
                        self._cleanup_failure(logical, cleanup)
                    except FilesystemAuditError as audit_error:
                        primary.add_note(
                            "cleanup audit delivery failed: "
                            f"{type(audit_error).__name__}"
                        )
                    primary.add_note("archive temporary cleanup failed")
                raise

        return self._mutate(
            "archive",
            logical,
            create_archive,
            attributes=attributes,
        )
