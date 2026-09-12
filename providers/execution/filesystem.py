"""Narrow, ticket-scoped audited filesystem mutations.

This boundary deliberately accepts rooted logical names, never arbitrary paths in
trace payloads.  Callers retain the physical :class:`Path` locally; traces retain
only owner-visible ``workspace://``/``artifact://``/``ticket://`` references.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import tarfile
import tempfile
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
    TraceEventV1,
    child_context,
    current_trace_context,
)


class FilesystemAuditError(RuntimeError):
    """Raised when a critical filesystem audit cannot be recorded."""


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
        value = str(relative).removeprefix(f"{self.scheme}://")
        target = (self.root / value).resolve()
        try:
            clean = target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("filesystem target escapes its audited root") from exc
        logical_parts = clean.parts
        if self.physical_prefix:
            prefix_parts = Path(self.physical_prefix).parts
            if logical_parts[: len(prefix_parts)] == prefix_parts:
                logical_parts = logical_parts[len(prefix_parts) :]
        rendered = "/".join((*Path(self.logical_prefix).parts, *logical_parts))
        return target, f"{self.scheme}://{rendered}"


class AuditedFilesystem:
    """Perform small local mutations with durable-before-mutation audit hooks.

    ``emit`` is intentionally synchronous: mutations occur in synchronous paths
    (ticket persistence and workspace construction), so awaiting an audit from an
    already-running event loop would otherwise be unsafe.  A raised emitter error
    prevents the mutation, keeping a successful mutation from existing without its
    required start record.  Event attributes contain no physical paths or content.
    """

    def __init__(
        self,
        root: RootedPath,
        *,
        ticket_id: str,
        emit: Callable[[TraceEventV1], Any] | None = None,
    ) -> None:
        self.root = root
        self.ticket_id = ticket_id
        self._emit = emit

    @staticmethod
    def _content_descriptor(content: bytes) -> dict[str, Any]:
        return {
            "size_bytes": len(content),
            "digest": hashlib.sha256(content).hexdigest(),
            "digest_kind": "sha256",
        }

    def _event(
        self,
        state: LifecycleState,
        operation: str,
        target: str,
        *,
        started: float,
        terminal: bool = False,
        **attributes: Any,
    ) -> TraceEventV1:
        context = current_trace_context()
        child = child_context(context) if context is not None else None
        return TraceEventV1(
            ticket_id=self.ticket_id,
            agent_id=context.agent_id if context else None,
            invocation_id=context.invocation_id if context else None,
            trace_id=child.trace_id if child else __import__("uuid").uuid4().hex,
            action_id=child.action_id if child else __import__("uuid").uuid4().hex[:16],
            parent_action_id=child.parent_action_id if child else None,
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
            error=attributes.pop("error", None),
            attributes={"operation": operation, "atomic": False, **attributes},
        )

    def _record(self, event: TraceEventV1) -> None:
        if self._emit is None:
            return
        try:
            result = self._emit(event)
            if inspect.isawaitable(result):
                raise FilesystemAuditError(
                    "filesystem audit emitter must be synchronous"
                )
        except Exception as exc:
            raise FilesystemAuditError("filesystem audit delivery failed") from exc

    def _mutate(
        self,
        operation: str,
        logical_target: str,
        action: Callable[[], Any],
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        started = time.monotonic()
        attrs = attributes or {}
        self._record(
            self._event(
                LifecycleState.REQUESTED,
                operation,
                logical_target,
                started=started,
                **attrs,
            )
        )
        try:
            result = action()
        except Exception as exc:
            self._record(
                self._event(
                    LifecycleState.FAILED,
                    operation,
                    logical_target,
                    started=started,
                    terminal=True,
                    **attrs,
                    error=ErrorDescriptor(type=type(exc).__name__, message=str(exc)),
                )
            )
            raise
        self._record(
            self._event(
                LifecycleState.COMPLETED,
                operation,
                logical_target,
                started=started,
                terminal=True,
                **attrs,
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
        existed = path.exists()
        operation = "replace" if existed else "create"
        descriptor = self._content_descriptor(data)
        descriptor.update(
            {"mode": oct(mode), "atomic": atomic, "write_kind": operation}
        )

        def write_file() -> Path:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not atomic:
                with open(path, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, mode)
                return path
            fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    os.chmod(temporary, mode)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                return path
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError as cleanup_error:
                    raise RuntimeError(
                        "temporary write cleanup failed"
                    ) from cleanup_error
                raise

        return self._mutate(operation, logical, write_file, attributes=descriptor)

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
        return self._mutate(
            "archive",
            logical,
            lambda: self._archive(target, resolved),
            attributes={
                "members": [reference for _, reference in resolved],
                "atomic": True,
            },
        )

    @staticmethod
    def _archive(target: Path, members: list[tuple[Path, str]]) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=".audit-", suffix=".tar.gz", dir=target.parent
        )
        os.close(fd)
        try:
            with tarfile.open(temporary, "w:gz") as archive:
                for path, reference in members:
                    archive.add(path, arcname=reference.split("://", 1)[1])
            os.replace(temporary, target)
            return target
        except Exception:
            try:
                os.unlink(temporary)
            except OSError as cleanup_error:
                raise RuntimeError(
                    "archive temporary cleanup failed"
                ) from cleanup_error
            raise
