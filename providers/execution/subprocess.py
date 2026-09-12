"""One safe, bounded and auditable local subprocess boundary."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import threading
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
from providers.tracing.client import TraceClient, TraceDeliveryError


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
        self._signal: str | None = None
        self._started = time.monotonic()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)

    async def _finish(self, state: LifecycleState, **descriptors: Any) -> None:
        if not self._terminal:
            self._terminal = True
            if self._signal is not None:
                descriptors["signal"] = self._signal
            await self._runner._record(
                self._runner._event(
                    state,
                    self._argv,
                    self.pid,
                    context=self._context,
                    returncode=self.returncode,
                    duration_ms=(time.monotonic() - self._started) * 1000,
                    **descriptors,
                )
            )

    async def _stop(self, task: asyncio.Task[Any]) -> None:
        """Bound shutdown so a TERM-ignoring child cannot defeat a timeout."""
        self.terminate()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._runner.shutdown_timeout
            )
        except asyncio.TimeoutError:
            self.kill()
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._runner.shutdown_timeout
            )

    async def wait(self, *, timeout: float | None = None) -> int:
        task = asyncio.create_task(self._process.wait())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            await self._stop(task)
            await self._finish(LifecycleState.TIMED_OUT, timed_out=True)
            raise
        except asyncio.CancelledError:
            await self._stop(task)
            await self._finish(LifecycleState.CANCELLED)
            raise
        await self._finish(
            LifecycleState.COMPLETED if result == 0 else LifecycleState.FAILED
        )
        return result

    def terminate(self) -> None:
        self._signal = "terminate"
        self._process.terminate()

    def kill(self) -> None:
        self._signal = "kill"
        self._process.kill()

    def send_signal(self, signal: int) -> None:
        self._signal = f"signal:{signal}"
        self._process.send_signal(signal)

    async def communicate(
        self, input: bytes | None = None, *, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        task = asyncio.create_task(self._process.communicate(input))
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            await self._stop(task)
            stdout, stderr = task.result()
            await self._finish(
                LifecycleState.TIMED_OUT,
                timed_out=True,
                **self._runner._output_descriptors(stdout, stderr),
            )
            raise
        except asyncio.CancelledError:
            await self._stop(task)
            stdout, stderr = task.result()
            await self._finish(
                LifecycleState.CANCELLED,
                **self._runner._output_descriptors(stdout, stderr),
            )
            raise
        await self._finish(
            LifecycleState.COMPLETED if self.returncode == 0 else LifecycleState.FAILED,
            **self._runner._output_descriptors(*result),
        )
        return result


class AuditedSubprocessRunner:
    """Runs argv-only local commands and emits start plus exactly one terminal event."""

    _default_recorder: TraceClient | None = None
    _default_recorder_lock = threading.Lock()

    def __init__(
        self,
        emit: Callable[[TraceEventV1], Awaitable[Any]] | None = None,
        *,
        recorder: TraceClient | None = None,
        output_limit: int = 65536,
        shutdown_timeout: float = 5.0,
    ) -> None:
        self._emit = emit
        self._recorder = recorder
        self._output_limit = output_limit
        self.shutdown_timeout = shutdown_timeout

    @classmethod
    def reset_default_recorder(cls) -> None:
        """Close ambient client state; primarily for orderly shutdown and tests."""
        with cls._default_recorder_lock:
            recorder, cls._default_recorder = cls._default_recorder, None
        if recorder is not None:
            recorder.close()

    def _output_descriptors(self, stdout: bytes, stderr: bytes) -> dict[str, Any]:
        return {
            "stdout_size": len(stdout),
            "stderr_size": len(stderr),
            "stdout_digest": hashlib.sha256(stdout).hexdigest()[:16],
            "stderr_digest": hashlib.sha256(stderr).hexdigest()[:16],
            "stdout_truncated": len(stdout) > self._output_limit,
            "stderr_truncated": len(stderr) > self._output_limit,
        }

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

    async def _record(
        self, event: TraceEventV1 | None, *, critical: bool = False
    ) -> None:
        if event is not None and self._emit is not None:
            await self._emit(event)
        elif event is not None:
            if self._recorder is None:
                url, token = (
                    os.environ.get("STATE_STORE_URL"),
                    os.environ.get("AGENTIC_PERF_API_TOKEN"),
                )
                if url and token:
                    with self._default_recorder_lock:
                        if self._default_recorder is None:
                            self._default_recorder = TraceClient(url, token)
                        self._recorder = self._default_recorder
            if self._recorder is None and critical:
                raise TraceDeliveryError(
                    "mutating subprocess requires central trace readiness"
                )
            if self._recorder is not None:
                method = (
                    self._recorder.record_critical
                    if critical
                    else self._recorder.record
                )
                await asyncio.to_thread(method, event)

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
        mutating: bool = False,
        system_context: bool = False,
    ) -> AuditedProcess:
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("argv must be a non-empty string sequence")
        context = current_trace_context()
        child = child_context(context) if context is not None else None
        requested = self._event(LifecycleState.REQUESTED, argv, context=child)
        if mutating and requested is None and not system_context:
            raise TraceDeliveryError(
                "mutating subprocess requires a ticket trace context"
            )
        await self._record(
            requested,
            critical=mutating,
        )
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
        tracked = AuditedProcess(self, process, argv, child)
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionError):
                tracked.terminate()
                await tracked._process.wait()
                await tracked._finish(LifecycleState.FAILED, stdin_error=True)
                raise
        return tracked

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        mutating: bool = False,
        system_context: bool = False,
        check: bool = False,
    ) -> ProcessResult:
        started = time.monotonic()
        process = await self.start(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            mutating=mutating,
            system_context=system_context,
        )
        timed_out = False
        task = asyncio.create_task(process._process.communicate())
        try:
            # Use the raw process once; the tracked facade owns the sole terminal
            # event after the controlling outcome (including timeout) is known.
            stdout, stderr = await asyncio.wait_for(asyncio.shield(task), timeout)
            await process._finish(
                LifecycleState.COMPLETED
                if process.returncode == 0
                else LifecycleState.FAILED,
                **self._output_descriptors(stdout, stderr),
            )
            outcome = "success" if process.returncode == 0 else "failure"
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await task
            await process._finish(
                LifecycleState.TIMED_OUT,
                timed_out=True,
                **self._output_descriptors(stdout, stderr),
            )
            outcome = "timed_out"
        except asyncio.CancelledError:
            process.terminate()
            await asyncio.shield(task)
            stdout, stderr = task.result()
            await process._finish(
                LifecycleState.CANCELLED,
                **self._output_descriptors(stdout, stderr),
            )
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
        if check and result.returncode:
            raise RuntimeError(f"command failed with exit {result.returncode}")
        return result

    def run_sync(self, argv: Sequence[str], **kwargs: Any) -> ProcessResult:
        """Compatibility bridge for non-async provider discovery helpers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(argv, **kwargs))

        # A few legacy discovery APIs are synchronous but are called by async
        # provider methods.  Running their audited coroutine on a short-lived
        # worker avoids nesting an event loop while retaining the caller's
        # trace context for the subprocess events.
        result: list[ProcessResult] = []
        error: list[BaseException] = []
        caller_context = contextvars.copy_context()

        def execute() -> None:
            try:
                result.append(caller_context.run(asyncio.run, self.run(argv, **kwargs)))
            except BaseException as exc:  # propagate the original provider error
                error.append(exc)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        worker.join()
        if error:
            raise error[0]
        return result[0]
