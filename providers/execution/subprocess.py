"""One safe, bounded and auditable local subprocess boundary."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    TraceEventV1,
    child_context,
    current_trace_context,
)


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    pid: int | None
    returncode: int | None
    stdout: bytes
    stderr: bytes
    outcome: str
    duration_ms: float
    timed_out: bool = False


class AuditedProcess:
    """Process facade that records exactly one terminal lifecycle event."""

    def __init__(
        self,
        runner: "AuditedSubprocessRunner",
        process: asyncio.subprocess.Process,
        argv: Sequence[str],
        context: Any,
    ) -> None:
        self._runner, self._process, self._argv, self._context = (
            runner,
            process,
            tuple(argv),
            context,
        )
        self._terminal = False
        self._started = time.monotonic()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)

    async def _finish(self, state: LifecycleState) -> None:
        if not self._terminal:
            self._terminal = True
            await self._runner._record(
                self._runner._event(
                    state,
                    self._argv,
                    self.pid,
                    context=self._context,
                    returncode=self.returncode,
                    duration_ms=(time.monotonic() - self._started) * 1000,
                )
            )

    async def wait(self) -> int:
        try:
            result = await self._process.wait()
        except asyncio.CancelledError:
            self.terminate()
            await self._process.wait()
            await self._finish(LifecycleState.CANCELLED)
            raise
        await self._finish(
            LifecycleState.COMPLETED if result == 0 else LifecycleState.FAILED
        )
        return result

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def send_signal(self, signal: int) -> None:
        self._process.send_signal(signal)

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        try:
            result = await self._process.communicate(input)
        except asyncio.CancelledError:
            self.terminate()
            await self._process.communicate()
            await self._finish(LifecycleState.CANCELLED)
            raise
        await self._finish(
            LifecycleState.COMPLETED if self.returncode == 0 else LifecycleState.FAILED
        )
        return result


class AuditedSubprocessRunner:
    """Runs argv-only local commands and emits start plus exactly one terminal event."""

    def __init__(
        self,
        emit: Callable[[TraceEventV1], Awaitable[Any]] | None = None,
        *,
        output_limit: int = 65536,
    ) -> None:
        self._emit = emit
        self._output_limit = output_limit

    def _event(
        self,
        state: LifecycleState,
        argv: Sequence[str],
        pid: int | None = None,
        context: Any | None = None,
        **attributes: Any,
    ) -> TraceEventV1 | None:
        context = context or current_trace_context()
        if context is None or not context.ticket_id:
            return None
        terminal = state in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.TIMED_OUT,
            LifecycleState.CANCELLED,
        }
        outcome = attributes.pop("outcome", None)
        return TraceEventV1(
            ticket_id=context.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            action=ActionDescriptor(type=ActionType.SUBPROCESS, target=argv[0]),
            lifecycle=LifecycleDescriptor(state=state),
            duration_ms=attributes.pop("duration_ms", 0) if terminal else None,
            outcome=OperationOutcome(outcome)
            if outcome
            else (
                OperationOutcome.CANCELLED
                if state == LifecycleState.CANCELLED
                else OperationOutcome.SUCCESS
                if state == LifecycleState.COMPLETED
                else OperationOutcome.TIMED_OUT
                if state == LifecycleState.TIMED_OUT
                else OperationOutcome.FAILURE
            )
            if terminal
            else None,
            attributes={
                "command": argv[0],
                "argv_count": len(argv),
                "argv_fingerprint": hashlib.sha256(
                    "\0".join(argv).encode()
                ).hexdigest()[:16],
                "pid": pid,
                **attributes,
            },
        )

    async def _record(self, event: TraceEventV1 | None) -> None:
        if event is not None and self._emit is not None:
            await self._emit(event)

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
    ) -> AuditedProcess:
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("argv must be a non-empty string sequence")
        context = current_trace_context()
        child = child_context(context) if context is not None else None
        await self._record(self._event(LifecycleState.REQUESTED, argv, context=child))
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=stdout,
                stderr=stderr,
            )
        except OSError:
            await self._record(
                self._event(
                    LifecycleState.FAILED,
                    argv,
                    context=child,
                    outcome="failure",
                    duration_ms=0,
                    spawn_error=True,
                )
            )
            raise
        await self._record(
            self._event(
                LifecycleState.STARTED,
                argv,
                process.pid,
                context=child,
                cwd=str(cwd) if cwd else None,
                env={
                    key: hashlib.sha256(value.encode()).hexdigest()[:16]
                    for key, value in (env or {}).items()
                },
                stdin_size=len(stdin or b""),
            )
        )
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
        return AuditedProcess(self, process, argv, child)

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> ProcessResult:
        started = time.monotonic()
        process = await self.start(argv, cwd=cwd, env=env, stdin=stdin)
        timed_out = False
        task = asyncio.create_task(process._process.communicate())
        try:
            # Use the raw process once; the tracked facade owns the sole terminal
            # event after the controlling outcome (including timeout) is known.
            stdout, stderr = await asyncio.wait_for(asyncio.shield(task), timeout)
            await process._finish(
                LifecycleState.COMPLETED
                if process.returncode == 0
                else LifecycleState.FAILED
            )
            outcome = "success" if process.returncode == 0 else "failure"
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await task
            await process._finish(LifecycleState.TIMED_OUT)
            outcome = "timed_out"
        except asyncio.CancelledError:
            process.terminate()
            await asyncio.shield(task)
            await process._finish(LifecycleState.CANCELLED)
            raise
        result = ProcessResult(
            tuple(argv),
            process.pid,
            process.returncode,
            stdout[: self._output_limit],
            stderr[: self._output_limit],
            outcome,
            (time.monotonic() - started) * 1000,
            timed_out,
        )
        return result

    def run_sync(self, argv: Sequence[str], **kwargs: Any) -> ProcessResult:
        """Compatibility bridge for non-async provider discovery helpers."""
        return asyncio.run(self.run(argv, **kwargs))
