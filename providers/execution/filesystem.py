"""Ticket-rooted filesystem mutations with durable, redacted audit records."""

from __future__ import annotations

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
from typing import Any

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    ErrorDescriptor,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    TraceContext,
    TraceEventV1,
    child_context,
    current_trace_context,
    new_trace_context,
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

    def resolve(self, relative: str | Path) -> tuple[Path, str]:
        target = (self.root / str(relative).removeprefix(f"{self.scheme}://")).resolve()
        try:
            clean = target.relative_to(self.root)
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
                    r"(?:secret|token|password|credential|private.?key)", part, re.I
                )
                else part
            )
            for part in parts
        ]
        rendered = "/".join((*Path(self.logical_prefix).parts, *safe_parts))
        return target, f"{self.scheme}://{rendered}"


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
        self.root, self.ticket_id, self._emit = root, ticket_id, emit
        self._critical = critical

    @staticmethod
    def _descriptor(data: bytes) -> dict[str, Any]:
        return {
            "size_bytes": len(data),
            "digest": hashlib.sha256(data).hexdigest(),
            "digest_kind": "sha256",
        }

    @staticmethod
    def _error(exc: BaseException) -> tuple[ErrorDescriptor, str]:
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

    def mkdir(self, relative: str | Path, *, mode: int = 0o700) -> Path:
        path, logical = self.root.resolve(relative)
        return self._mutate(
            "mkdir",
            logical,
            lambda: (path.mkdir(parents=True, exist_ok=True, mode=mode), path)[1],
            attributes={"mode": oct(mode)},
        )

    def write(
        self,
        relative: str | Path,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
        mode: int = 0o600,
        atomic: bool = True,
    ) -> Path:
        path, logical = self.root.resolve(relative)
        data = content if isinstance(content, bytes) else content.encode(encoding)
        operation = "replace" if path.exists() else "create"
        attributes = self._descriptor(data) | {
            "mode": oct(mode),
            "atomic": atomic,
            "write_kind": operation,
        }

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
                    os.chmod(temporary or path, mode)
                    if handle.write(data) != len(data):
                        raise OSError("short filesystem write")
                    handle.flush()
                    os.fsync(handle.fileno())
                if temporary:
                    os.replace(temporary, path)
                    temporary = None
                if self._descriptor(path.read_bytes()) != self._descriptor(data):
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
            lambda: (source_path.rename(destination_path), destination_path)[1],
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
        resolved = [self.root.resolve(member) for member in members]

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
            attributes={
                "members": [reference for _, reference in resolved],
                "atomic": True,
            },
        )
