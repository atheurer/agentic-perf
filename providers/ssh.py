from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    PayloadDescriptor,
    TraceContext,
    TraceEventV1,
    child_context,
    current_trace_context,
)

logger = logging.getLogger(__name__)

_PID_SENTINEL = "__PID:"
_PID_RE = re.compile(r"__PID:(\d+)$", re.MULTILINE)


def parse_pid_sentinel(stdout: str) -> int | None:
    """Extract the PID from a sentinel line like ``__PID:12345``."""
    m = _PID_RE.search(stdout)
    return int(m.group(1)) if m else None


class SSHKeyResolutionError(Exception):
    """Raised when vault-fallback SSH key resolution is configured but fails.

    This is a fail-closed error: the vault secret name was specified
    (via ticket ``ssh_key_secret``, env ``SSH_KEY_VAULT_SECRET``, or
    config ``ssh_key_vault_secret``) but the secret was not found in
    the provider.  Silently falling back to no key would let SSH try
    default identity files, which is a security risk.
    """


@dataclass
class SSHResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class _ProgressState:
    """Per-task state that lets the public progress wrapper close safely."""

    action: "_SSHTraceAction"
    child: Callable[[], "SSHExecutor"]
    host: str
    run_dir: str | None
    key_path: str | None
    remote_pid: int | None = None


_PROGRESS_STATE: ContextVar[_ProgressState | None] = ContextVar(
    "agentic_perf_progress_state", default=None
)


class _SSHTraceAction:
    """One immutable child action with a request/start/terminal lifecycle."""

    def __init__(self, executor: "SSHExecutor", kind: str, host: str) -> None:
        parent = executor.trace_context or current_trace_context()
        self.executor = executor
        self.kind = kind
        self.host = host
        self.context = child_context(parent) if parent is not None else None
        self.closed = False

    def record(self, state: LifecycleState, **attributes: Any) -> None:
        if self.context is None or self.executor.trace_recorder is None:
            return
        terminal = state in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.TIMED_OUT,
            LifecycleState.CANCELLED,
        }
        event = TraceEventV1(
            ticket_id=self.context.ticket_id,
            agent_id=self.context.agent_id,
            invocation_id=self.context.invocation_id,
            trace_id=self.context.trace_id,
            action_id=self.context.action_id,
            parent_action_id=self.context.parent_action_id,
            action=ActionDescriptor(
                type=ActionType.SSH, phase=self.kind, target=self.host
            ),
            lifecycle=LifecycleDescriptor(state=state),
            outcome=(
                OperationOutcome.SUCCESS
                if state == LifecycleState.COMPLETED
                else OperationOutcome.CANCELLED
                if state == LifecycleState.CANCELLED
                else OperationOutcome.TIMED_OUT
                if state == LifecycleState.TIMED_OUT
                else OperationOutcome.FAILURE
                if terminal
                else None
            ),
            duration_ms=attributes.pop("duration_ms", 0) if terminal else None,
            attributes=attributes,
        )
        recorder = getattr(
            self.executor.trace_recorder, "record_critical", None
        ) or getattr(self.executor.trace_recorder, "insert_event", None)
        if recorder is None:
            raise RuntimeError("SSH trace recorder lacks critical persistence")
        recorder(event)

    def terminal(self, state: LifecycleState, **attributes: Any) -> None:
        if not self.closed:
            self.closed = True
            self.record(state, **attributes)


class SSHExecutor:
    def __init__(
        self,
        user: str = "root",
        key_path: str | None = None,
        connect_timeout: int = 10,
        strict_host_key: str = "accept-new",
        trace_context: TraceContext | None = None,
        trace_recorder: Any | None = None,
    ) -> None:
        self.user = user
        self.key_path = key_path
        self.connect_timeout = connect_timeout
        self.strict_host_key = strict_host_key
        self.trace_context = trace_context
        self.trace_recorder = trace_recorder

    def _trace(
        self, kind: str, state: LifecycleState, host: str, **attributes: Any
    ) -> None:
        """Record bounded SSH metadata; commands and stream contents never persist."""
        context = self.trace_context or current_trace_context()
        if self.trace_recorder is None or context is None or not context.ticket_id:
            return
        event = TraceEventV1(
            ticket_id=context.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            iteration=context.iteration,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            tool_call_id=context.tool_call_id,
            action=ActionDescriptor(type=ActionType.SSH, phase=kind, target=host),
            lifecycle=LifecycleDescriptor(state=state),
            attributes=attributes,
        )
        recorder = getattr(self.trace_recorder, "record_critical", None)
        if recorder is None:
            recorder = getattr(self.trace_recorder, "insert_event", None)
        if recorder is None:
            raise RuntimeError("SSH trace recorder lacks critical persistence")
        recorder(event)

    @staticmethod
    def _digest(value: str | bytes | None) -> str | None:
        if value is None:
            return None
        raw = value.encode() if isinstance(value, str) else value
        return hashlib.sha256(raw).hexdigest()

    def _ssh_args(
        self,
        host: str,
        key_path: str | None = None,
        allocate_pty: bool = False,
    ) -> list[str]:
        args = [
            "ssh",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        # When strict checking is disabled (Jumpstarter
        # boards get reflashed constantly), also ignore
        # the known_hosts file. Otherwise a stale entry
        # from a previous flash causes BatchMode=yes to
        # reject the connection even with
        # StrictHostKeyChecking=no.
        if self.strict_host_key == "no":
            args.extend(["-o", "UserKnownHostsFile=/dev/null"])
        if allocate_pty:
            args.append("-tt")
        effective_key = key_path or self.key_path
        if effective_key:
            args.extend(["-i", effective_key])
        args.append(f"{self.user}@{host}")
        return args

    async def run(
        self,
        host: str,
        command: str,
        timeout: int = 300,
        key_path: str | None = None,
        allocate_pty: bool = False,
        stdin_data: bytes | None = None,
        mutating: bool = False,
    ) -> SSHResult:
        args = self._ssh_args(host, key_path=key_path, allocate_pty=allocate_pty) + [
            command
        ]
        started = time.monotonic()
        trace = _SSHTraceAction(self, "ssh", host)
        if mutating and (trace.context is None or self.trace_recorder is None):
            raise RuntimeError("mutating SSH requires durable trace readiness")
        trace.record(
            LifecycleState.REQUESTED,
            user=self.user,
            timeout=timeout,
            command_digest=self._digest(command),
            stdin=PayloadDescriptor(
                digest=self._digest(stdin_data),
                digest_kind="sha256",
                size_bytes=len(stdin_data),
            )
            if stdin_data is not None
            else None,
            key_identity=self._digest(key_path or self.key_path),
        )
        logger.info(f"[ssh] {self.user}@{host}: {command[:120]}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=(asyncio.subprocess.PIPE if stdin_data is not None else None),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException as exc:
            trace.terminal(
                LifecycleState.FAILED,
                launch_failed=True,
                error_type=type(exc).__name__,
            )
            raise
        trace.record(LifecycleState.LAUNCHED, local_pid=proc.pid)

        try:
            coro = proc.communicate(input=stdin_data)
            if timeout > 0:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    coro, timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes = await coro
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            result = SSHResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
            )
            trace.terminal(
                LifecycleState.TIMED_OUT,
                exit_code=-1,
                timeout=True,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return result
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            trace.terminal(
                LifecycleState.CANCELLED,
                local_pid=proc.pid,
                cancelled=True,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise

        result = SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )

        if result.exit_code != 0:
            caller = "".join(traceback.format_stack(limit=4)[:-1]).strip()
            logger.warning(
                f"[ssh] {host} exit={result.exit_code}: {result.stderr[:200]}"
                f"\n  caller: {caller}"
            )

        trace.terminal(
            LifecycleState.COMPLETED
            if result.exit_code == 0
            else LifecycleState.FAILED,
            local_pid=proc.pid,
            remote_pid=None,
            capture_status="not_requested",
            exit_code=result.exit_code,
            duration_ms=(time.monotonic() - started) * 1000,
            stdout_digest=self._digest(result.stdout),
            stderr_digest=self._digest(result.stderr),
        )

        return result

    _MAX_SSH_POLL_FAILURES = 10

    async def run_with_progress(
        self,
        host: str,
        command: str,
        progress_callback: Callable[[str, int], Awaitable[None]] | None = None,
        poll_interval: int = 30,
        key_path: str | None = None,
    ) -> SSHResult:
        """Run progress workflow while closing its audit lifecycle on every exit."""
        token = _PROGRESS_STATE.set(None)
        state: _ProgressState | None = None
        result: SSHResult | None = None
        primary_error: BaseException | None = None
        try:
            result = await self._run_with_progress_impl(
                host, command, progress_callback, poll_interval, key_path
            )
            return result
        except asyncio.CancelledError:
            state = _PROGRESS_STATE.get()
            if state is not None:
                state.action.terminal(
                    LifecycleState.CANCELLED,
                    remote_pid=state.remote_pid,
                    capture_status="captured",
                    cancelled=True,
                )
            raise
        except BaseException as exc:
            primary_error = exc
            state = _PROGRESS_STATE.get()
            if state is not None:
                state.action.terminal(
                    LifecycleState.FAILED,
                    remote_pid=state.remote_pid,
                    capture_status="captured",
                    error_type=type(exc).__name__,
                )
            raise
        finally:
            state = state or _PROGRESS_STATE.get()
            if state is not None and state.run_dir is not None:
                cleanup_failed = False
                cleanup_exit_code: int | None = None
                try:
                    cleanup = await state.child().run(
                        state.host,
                        f"rm -rf {state.run_dir}",
                        timeout=5,
                        key_path=state.key_path,
                        # Test/dry-run instance overrides predate the audit
                        # keyword. Real child executors fail closed here.
                        **({"mutating": True} if "run" not in self.__dict__ else {}),
                    )
                    if cleanup.exit_code != 0:
                        cleanup_failed = True
                        cleanup_exit_code = cleanup.exit_code
                        if primary_error is None and result is not None:
                            result.stderr = (
                                f"{result.stderr}; {cleanup.stderr or 'Cleanup failed'}"
                            ).strip("; ")
                        logger.warning("[ssh] %s: capture cleanup failed", state.host)
                except BaseException as cleanup_error:
                    cleanup_failed = True
                    if primary_error is None and result is not None:
                        result.stderr = (
                            f"{result.stderr}; cleanup failed: {cleanup_error}"
                        ).strip("; ")
                    logger.warning(
                        "[ssh] %s: capture cleanup raised: %s",
                        state.host,
                        cleanup_error,
                    )
                if not state.action.closed and result is not None:
                    state.action.terminal(
                        LifecycleState.COMPLETED
                        if result.exit_code == 0 and not cleanup_failed
                        else LifecycleState.FAILED,
                        remote_pid=state.remote_pid,
                        capture_status="captured",
                        exit_code=result.exit_code,
                        cleanup_failed=cleanup_failed,
                        cleanup_exit_code=cleanup_exit_code,
                        output_digest=self._digest(result.stdout),
                    )
            _PROGRESS_STATE.reset(token)

    async def _run_with_progress_impl(
        self,
        host: str,
        command: str,
        progress_callback: Callable[[str, int], Awaitable[None]] | None = None,
        poll_interval: int = 30,
        key_path: str | None = None,
    ) -> SSHResult:
        """Run a long-running command with periodic progress callbacks.

        Launches the command in the background on the remote host,
        polls its output file periodically, and invokes the callback
        with the last new output line and elapsed seconds. Only calls
        the callback when output has changed since the last poll.

        Returns the same SSHResult as run() with the full output.
        """
        progress = _SSHTraceAction(self, "ssh_progress", host)

        def child() -> SSHExecutor:
            # Preserve instance-level test/dry-run overrides of ``run``. Real
            # executors receive a causally-linked child action.
            if "run" in self.__dict__:
                return self
            context = (
                child_context(progress.context)
                if progress.context is not None
                else None
            )
            return SSHExecutor(
                user=self.user,
                key_path=self.key_path,
                connect_timeout=self.connect_timeout,
                strict_host_key=self.strict_host_key,
                trace_context=context,
                trace_recorder=self.trace_recorder,
            )

        progress.record(
            LifecycleState.REQUESTED,
            command_digest=self._digest(command),
            capture_status="pending",
        )
        _PROGRESS_STATE.set(_ProgressState(progress, child, host, None, key_path))
        mkd = await child().run(
            host,
            "mktemp -d /tmp/run-XXXXXXXX",
            timeout=10,
            key_path=key_path,
            **({"mutating": True} if "run" not in self.__dict__ else {}),
        )
        if mkd.exit_code != 0 or not mkd.stdout.strip():
            progress.terminal(
                LifecycleState.FAILED,
                capture_status="mktemp_failed",
                duration_ms=0,
            )
            return SSHResult(
                stdout=mkd.stdout or "",
                stderr=mkd.stderr or "Failed to create temp directory",
                exit_code=mkd.exit_code or 1,
            )
        run_dir = mkd.stdout.strip()
        out_file = f"{run_dir}/out"
        rc_file = f"{run_dir}/rc"
        state = _PROGRESS_STATE.get()
        if state is not None:
            state.run_dir = run_dir

        escaped = command.replace("'", "'\\''")
        bg_cmd = (
            f"nohup sh -c '{escaped}; echo $? > {rc_file}'"
            f" > {out_file} 2>&1 & echo {_PID_SENTINEL}$!"
        )
        launch = await child().run(
            host,
            bg_cmd,
            timeout=30,
            key_path=key_path,
            **({"mutating": True} if "run" not in self.__dict__ else {}),
        )
        pid = parse_pid_sentinel(launch.stdout or "")
        if launch.exit_code != 0 or pid is None:
            progress.terminal(
                LifecycleState.FAILED,
                capture_status="missing" if pid is None else "launch_failed",
                exit_code=launch.exit_code,
            )
            return SSHResult(
                stdout=launch.stdout or "",
                stderr=launch.stderr or "Failed to launch background command",
                exit_code=launch.exit_code or 1,
            )
        logger.info(f"[ssh] {host}: background pid={pid} for: {command[:120]}")
        state = _PROGRESS_STATE.get()
        if state is not None:
            state.remote_pid = pid
        progress.record(
            LifecycleState.LAUNCHED,
            remote_pid=pid,
            capture_status="captured",
        )

        last_reported = ""
        elapsed = 0
        consecutive_ssh_failures = 0

        while True:
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                progress.terminal(
                    LifecycleState.CANCELLED,
                    remote_pid=pid,
                    capture_status="captured",
                    cancelled=True,
                )
                raise
            elapsed += poll_interval

            done_check = await child().run(
                host,
                f"test -f {rc_file}",
                timeout=5,
                key_path=key_path,
            )

            # SSH connection failure (exit 255) or timeout (exit -1)
            # is distinct from "test -f" returning 1 (file not found).
            if done_check.exit_code in (-1, 255):
                consecutive_ssh_failures += 1
                if consecutive_ssh_failures >= self._MAX_SSH_POLL_FAILURES:
                    logger.error(
                        f"[ssh] {host}: {consecutive_ssh_failures} consecutive SSH"
                        f" failures polling pid={pid}, giving up"
                    )
                    progress.terminal(
                        LifecycleState.FAILED,
                        remote_pid=pid,
                        capture_status="captured",
                        poll_failures=consecutive_ssh_failures,
                        exit_code=1,
                    )
                    return SSHResult(
                        stdout="",
                        stderr=(
                            f"Lost SSH connectivity to {host} after"
                            f" {consecutive_ssh_failures} consecutive failures"
                        ),
                        exit_code=1,
                    )
                logger.warning(
                    f"[ssh] {host}: SSH poll failure #{consecutive_ssh_failures}"
                    f" for pid={pid}"
                )
                continue
            else:
                consecutive_ssh_failures = 0

            finished = done_check.exit_code == 0

            if progress_callback:
                tail = await child().run(
                    host,
                    f"tail -5 {out_file} 2>/dev/null",
                    timeout=10,
                    key_path=key_path,
                )
                lines = [ln for ln in (tail.stdout or "").splitlines() if ln.strip()]
                last_line = lines[-1] if lines else ""
                if last_line and last_line != last_reported:
                    last_reported = last_line
                    callback_trace = _SSHTraceAction(
                        SSHExecutor(
                            user=self.user,
                            key_path=self.key_path,
                            connect_timeout=self.connect_timeout,
                            strict_host_key=self.strict_host_key,
                            trace_context=progress.context,
                            trace_recorder=self.trace_recorder,
                        ),
                        "ssh_progress_callback",
                        host,
                    )
                    callback_trace.record(
                        LifecycleState.REQUESTED,
                        remote_pid=pid,
                    )
                    try:
                        await progress_callback(last_line, elapsed)
                    except Exception as exc:
                        # Callback failures must be visible even though the
                        # remote command continues and retains its API result.
                        callback_trace.terminal(
                            LifecycleState.FAILED,
                            remote_pid=pid,
                            error_type=type(exc).__name__,
                        )
                    else:
                        callback_trace.terminal(
                            LifecycleState.COMPLETED,
                            remote_pid=pid,
                        )

            if finished:
                break

        full_output = await child().run(
            host,
            f"cat {out_file}",
            timeout=60,
            key_path=key_path,
        )
        if full_output.exit_code != 0:
            progress.terminal(
                LifecycleState.FAILED,
                remote_pid=pid,
                capture_status="captured",
                output_collection_failed=True,
                exit_code=full_output.exit_code,
            )
            return full_output
        rc_output = await child().run(
            host,
            f"cat {rc_file}",
            timeout=5,
            key_path=key_path,
        )
        try:
            exit_code = int(rc_output.stdout.strip())
        except (TypeError, ValueError):
            progress.terminal(
                LifecycleState.FAILED,
                remote_pid=pid,
                capture_status="captured",
                rc_collection_failed=True,
            )
            return SSHResult(
                full_output.stdout or "", rc_output.stderr or "Invalid rc", 1
            )

        result = SSHResult(
            stdout=full_output.stdout or "",
            stderr="",
            exit_code=exit_code,
        )
        return result

    async def copy_from(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        timeout: int = 120,
        key_path: str | None = None,
        mutating: bool = False,
    ) -> SSHResult:
        started = time.monotonic()
        trace = _SSHTraceAction(self, "scp_from", host)
        if mutating and (trace.context is None or self.trace_recorder is None):
            raise RuntimeError("mutating SCP requires durable trace readiness")
        trace.record(
            LifecycleState.REQUESTED,
            user=self.user,
            timeout=timeout,
            remote_path_digest=self._digest(remote_path),
            local_path_digest=self._digest(local_path),
            key_identity=self._digest(key_path or self.key_path),
        )
        args = [
            "scp",
            "-r",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key}",
        ]
        if self.strict_host_key == "no":
            args.extend(["-o", "UserKnownHostsFile=/dev/null"])
        effective_key = key_path or self.key_path
        if effective_key:
            args.extend(["-i", effective_key])
        args.extend([f"{self.user}@{host}:{remote_path}", local_path])

        logger.info(f"[scp] {self.user}@{host}:{remote_path} -> {local_path}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except BaseException as exc:
            trace.terminal(
                LifecycleState.FAILED,
                launch_failed=True,
                error_type=type(exc).__name__,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise
        trace.record(LifecycleState.LAUNCHED, local_pid=proc.pid)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            result = SSHResult(
                stdout="", stderr=f"SCP timed out after {timeout}s", exit_code=-1
            )
            trace.terminal(
                LifecycleState.TIMED_OUT,
                local_pid=proc.pid,
                exit_code=-1,
                timeout=True,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return result
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            trace.terminal(
                LifecycleState.CANCELLED,
                local_pid=proc.pid,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise

        result = SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
        trace.terminal(
            LifecycleState.COMPLETED
            if result.exit_code == 0
            else LifecycleState.FAILED,
            local_pid=proc.pid,
            exit_code=result.exit_code,
            duration_ms=(time.monotonic() - started) * 1000,
            stdout_digest=self._digest(result.stdout),
            stderr_digest=self._digest(result.stderr),
        )
        return result

    async def copy_to(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
        key_path: str | None = None,
        mutating: bool = False,
    ) -> SSHResult:
        started = time.monotonic()
        trace = _SSHTraceAction(self, "scp_to", host)
        if mutating and (trace.context is None or self.trace_recorder is None):
            raise RuntimeError("mutating SCP requires durable trace readiness")
        trace.record(
            LifecycleState.REQUESTED,
            user=self.user,
            timeout=timeout,
            remote_path_digest=self._digest(remote_path),
            local_path_digest=self._digest(local_path),
            key_identity=self._digest(key_path or self.key_path),
        )
        args = [
            "scp",
            "-r",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key}",
        ]
        if self.strict_host_key == "no":
            args.extend(["-o", "UserKnownHostsFile=/dev/null"])
        effective_key = key_path or self.key_path
        if effective_key:
            args.extend(["-i", effective_key])
        args.extend([local_path, f"{self.user}@{host}:{remote_path}"])

        logger.info(f"[scp] {local_path} -> {self.user}@{host}:{remote_path}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except BaseException as exc:
            trace.terminal(
                LifecycleState.FAILED,
                launch_failed=True,
                error_type=type(exc).__name__,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise
        trace.record(LifecycleState.LAUNCHED, local_pid=proc.pid)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            result = SSHResult(
                stdout="", stderr=f"SCP timed out after {timeout}s", exit_code=-1
            )
            trace.terminal(
                LifecycleState.TIMED_OUT,
                local_pid=proc.pid,
                exit_code=-1,
                timeout=True,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return result
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            trace.terminal(
                LifecycleState.CANCELLED,
                local_pid=proc.pid,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise

        result = SSHResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
        trace.terminal(
            LifecycleState.COMPLETED
            if result.exit_code == 0
            else LifecycleState.FAILED,
            local_pid=proc.pid,
            exit_code=result.exit_code,
            duration_ms=(time.monotonic() - started) * 1000,
            stdout_digest=self._digest(result.stdout),
            stderr_digest=self._digest(result.stderr),
        )
        return result
