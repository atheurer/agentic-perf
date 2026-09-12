"""Regression coverage for bounded, correlated SSH trace metadata."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from providers.ssh import SSHExecutor, SSHResult
from providers.tracing import LifecycleState, new_trace_context
from state_store.trace_store import TraceStore


def test_ssh_trace_is_correlated_and_never_contains_command_secret(
    tmp_path: Path,
) -> None:
    secret = "do-not-persist"
    with TraceStore(tmp_path / "trace.db") as store:
        executor = SSHExecutor(
            trace_context=new_trace_context(ticket_id="PERF-SSH", agent_id="agent"),
            trace_recorder=store,
        )
        executor._trace(
            "ssh",
            LifecycleState.REQUESTED,
            "host.example",
            command_digest=executor._digest(f"echo {secret}"),
            stdin_digest=executor._digest(secret),
        )
        event = store.list_events("PERF-SSH")[0]
        assert event.parent_action_id is None
        assert secret not in event.model_dump_json()
        assert event.attributes["command_digest"]


def test_trace_recorder_failure_blocks_audit_enabled_launch(tmp_path: Path) -> None:
    class BrokenRecorder:
        def record_critical(self, _: object) -> None:
            raise RuntimeError("trace unavailable")

    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=BrokenRecorder(),
    )
    try:
        executor._trace("ssh", LifecycleState.REQUESTED, "host")
    except RuntimeError as exc:
        assert "trace unavailable" in str(exc)
    else:  # pragma: no cover - explicit critical persistence guard
        raise AssertionError("critical trace failure must block launch")


async def test_progress_missing_pid_records_closed_failure(tmp_path: Path) -> None:
    events: list[object] = []

    class Recorder:
        def record_critical(self, event: object) -> None:
            events.append(event)

    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"), trace_recorder=Recorder()
    )
    calls = 0

    async def fake_run(*_: object, **__: object):
        nonlocal calls
        from providers.ssh import SSHResult

        calls += 1
        return SSHResult("/tmp/run\n", "", 0) if calls == 1 else SSHResult("bad", "", 0)

    executor.run = fake_run
    result = await executor.run_with_progress("host", "command")
    assert result.exit_code == 1
    progress = [event for event in events if event.action.phase == "ssh_progress"]
    assert progress[-1].lifecycle.state == LifecycleState.FAILED
    assert progress[-1].attributes["capture_status"] == "missing"


async def test_progress_poll_loss_closes_parent_failure() -> None:
    events: list[object] = []

    class Recorder:
        def record_critical(self, event: object) -> None:
            events.append(event)

    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"), trace_recorder=Recorder()
    )
    calls = 0

    async def fake_run(*_: object, **__: object):
        nonlocal calls
        from providers.ssh import SSHResult

        calls += 1
        if calls == 1:
            return SSHResult("/tmp/run\n", "", 0)
        if calls == 2:
            return SSHResult("__PID:9\n", "", 0)
        return SSHResult("", "lost", 255)

    executor.run = fake_run
    import asyncio

    original_sleep = asyncio.sleep
    asyncio.sleep = lambda _: original_sleep(0)
    try:
        result = await executor.run_with_progress("host", "command", poll_interval=1)
    finally:
        asyncio.sleep = original_sleep
    assert result.exit_code == 1
    progress = [event for event in events if event.action.phase == "ssh_progress"]
    assert progress[-1].lifecycle.state == LifecycleState.FAILED


async def test_progress_output_collection_failure_closes_parent() -> None:
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, e: events.append(e)}
        )(),
    )
    from providers.ssh import SSHResult

    replies = iter(
        [
            SSHResult("/tmp/run\n", "", 0),
            SSHResult("__PID:9\n", "", 0),
            SSHResult("", "", 0),
            SSHResult("", "read failed", 255),
        ]
    )

    async def fake_run(*_: object, **__: object):
        return next(replies)

    executor.run = fake_run
    import asyncio

    original = asyncio.sleep
    asyncio.sleep = lambda _: original(0)
    try:
        result = await executor.run_with_progress("host", "command", poll_interval=1)
    finally:
        asyncio.sleep = original
    assert result.exit_code == 255
    assert [e for e in events if e.action.phase == "ssh_progress"][-1].attributes[
        "output_collection_failed"
    ]


async def test_progress_cleanup_failure_closes_parent() -> None:
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, e: events.append(e)}
        )(),
    )
    from providers.ssh import SSHResult

    replies = iter(
        [
            SSHResult("/tmp/run\n", "", 0),
            SSHResult("__PID:9\n", "", 0),
            SSHResult("", "", 0),
            SSHResult("out", "", 0),
            SSHResult("0", "", 0),
            SSHResult("", "rm failed", 1),
        ]
    )

    async def fake_run(*_: object, **__: object):
        return next(replies)

    executor.run = fake_run
    import asyncio

    original = asyncio.sleep
    asyncio.sleep = lambda _: original(0)
    try:
        result = await executor.run_with_progress("host", "command", poll_interval=1)
    finally:
        asyncio.sleep = original
    assert result.stderr == "rm failed"
    assert [e for e in events if e.action.phase == "ssh_progress"][-1].attributes[
        "cleanup_failed"
    ]


@pytest.mark.asyncio
async def test_progress_mktemp_cancellation_closes_parent() -> None:
    """Cancellation before the capture directory exists still closes progress."""
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )

    async def cancelled(*_: object, **__: object) -> SSHResult:
        raise asyncio.CancelledError

    executor.run = cancelled
    with pytest.raises(asyncio.CancelledError):
        await executor.run_with_progress("host", "command")

    progress = [event for event in events if event.action.phase == "ssh_progress"]
    assert [event.lifecycle.state for event in progress] == [
        LifecycleState.REQUESTED,
        LifecycleState.CANCELLED,
    ]


async def test_progress_callback_failure_has_closed_child_lifecycle() -> None:
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    from providers.ssh import SSHResult

    replies = iter(
        [
            SSHResult("/tmp/run\n", "", 0),
            SSHResult("__PID:9\n", "", 0),
            SSHResult("", "", 0),
            SSHResult("progress\n", "", 0),
            SSHResult("output", "", 0),
            SSHResult("3", "", 0),
            SSHResult("", "", 0),
        ]
    )

    async def fake_run(*_: object, **__: object) -> SSHResult:
        return next(replies)

    async def broken_callback(*_: object) -> None:
        raise RuntimeError("callback failure")

    executor.run = fake_run
    import asyncio

    original = asyncio.sleep
    asyncio.sleep = lambda _: original(0)
    try:
        result = await executor.run_with_progress(
            "host", "command", broken_callback, poll_interval=1
        )
    finally:
        asyncio.sleep = original
    assert result.exit_code == 3
    callback = [e for e in events if e.action.phase == "ssh_progress_callback"]
    assert [e.lifecycle.state for e in callback] == [
        LifecycleState.REQUESTED,
        LifecycleState.FAILED,
    ]
    assert callback[0].action_id == callback[1].action_id
    progress = [e for e in events if e.action.phase == "ssh_progress"]
    assert progress[-1].lifecycle.state == LifecycleState.FAILED


@pytest.mark.parametrize("method", ["copy_to", "copy_from"])
@pytest.mark.parametrize(
    ("exit_code", "terminal"),
    [
        (0, LifecycleState.COMPLETED),
        (1, LifecycleState.FAILED),
        (255, LifecycleState.FAILED),
    ],
)
async def test_scp_transfer_audits_success_and_remote_failures(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    exit_code: int,
    terminal: LifecycleState,
) -> None:
    events: list[object] = []

    class Process:
        pid = 4242
        returncode = exit_code

        async def communicate(self):
            return b"output", b"failure"

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("providers.ssh.asyncio.create_subprocess_exec", spawn)
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SCP"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    result = await getattr(executor, method)("host", "source", "destination")
    assert result.exit_code == exit_code
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.REQUESTED,
        LifecycleState.LAUNCHED,
        terminal,
    ]
    assert events[-1].attributes["local_pid"] == 4242
    assert "source" not in str(events)
    assert "destination" not in str(events)


async def test_mutating_ssh_fails_closed_without_durable_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not spawn before critical audit readiness")

    monkeypatch.setattr("providers.ssh.asyncio.create_subprocess_exec", must_not_spawn)
    executor = SSHExecutor(trace_context=new_trace_context(ticket_id="PERF-SSH"))
    with pytest.raises(RuntimeError, match="durable trace readiness"):
        await executor.run("host", "touch /mutating", mutating=True)


@pytest.mark.parametrize("exit_code", [0, 1, 255])
async def test_ssh_run_audits_success_and_remote_failures(
    monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    """SSH records request, spawn, and exactly one terminal for all RCs."""
    events: list[object] = []

    class Process:
        pid = 17
        returncode = exit_code

        async def communicate(self, input=None):
            return b"visible-output", b"visible-error"

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("providers.ssh.asyncio.create_subprocess_exec", spawn)
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    result = await executor.run("host", "command")
    assert result.exit_code == exit_code
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.REQUESTED,
        LifecycleState.LAUNCHED,
        LifecycleState.COMPLETED if exit_code == 0 else LifecycleState.FAILED,
    ]
    assert events[-1].attributes["local_pid"] == 17


async def test_ssh_run_timeout_cancellation_and_launch_failure_are_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout, task cancellation, and spawn errors each close the action."""

    class Process:
        pid = 18
        returncode = None

        async def communicate(self, input=None):
            await asyncio.Event().wait()

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> None:
            return None

    for mode, expected in (
        ("timeout", LifecycleState.TIMED_OUT),
        ("cancel", LifecycleState.CANCELLED),
        ("launch", LifecycleState.FAILED),
    ):
        events: list[object] = []

        async def spawn(*_args: object, **_kwargs: object) -> Process:
            if mode == "launch":
                raise OSError("spawn failed")
            return Process()

        monkeypatch.setattr("providers.ssh.asyncio.create_subprocess_exec", spawn)
        executor = SSHExecutor(
            trace_context=new_trace_context(ticket_id="PERF-SSH"),
            trace_recorder=type(
                "Recorder",
                (),
                {"record_critical": lambda _, event: events.append(event)},
            )(),
        )
        if mode == "timeout":
            assert (await executor.run("host", "command", timeout=0.01)).exit_code == -1
        elif mode == "cancel":
            task = asyncio.create_task(executor.run("host", "command"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(OSError, match="spawn failed"):
                await executor.run("host", "command")
        assert events[-1].lifecycle.state == expected


@pytest.mark.parametrize("method", ["copy_to", "copy_from"])
@pytest.mark.parametrize("mode", ["timeout", "cancel", "launch"])
async def test_scp_timeout_cancellation_and_launch_failure_are_audited(
    monkeypatch: pytest.MonkeyPatch, method: str, mode: str
) -> None:
    events: list[object] = []

    class Process:
        pid = 19
        returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> None:
            return None

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        if mode == "launch":
            raise OSError("spawn failed")
        return Process()

    monkeypatch.setattr("providers.ssh.asyncio.create_subprocess_exec", spawn)
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SCP"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    call = getattr(executor, method)
    if mode == "timeout":
        assert (
            await call("host", "source", "destination", timeout=0.01)
        ).exit_code == -1
        expected = LifecycleState.TIMED_OUT
    elif mode == "cancel":
        task = asyncio.create_task(call("host", "source", "destination"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        expected = LifecycleState.CANCELLED
    else:
        with pytest.raises(OSError, match="spawn failed"):
            await call("host", "source", "destination")
        expected = LifecycleState.FAILED
    assert events[-1].lifecycle.state == expected


async def test_progress_callback_success_retains_progress_ancestor() -> None:
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    replies = iter(
        [
            SSHResult("/tmp/run\n", "", 0),
            SSHResult("__PID:9\n", "", 0),
            SSHResult("", "", 0),
            SSHResult("progress\n", "", 0),
            SSHResult("output", "", 0),
            SSHResult("0", "", 0),
            SSHResult("", "", 0),
        ]
    )
    observed: list[str] = []

    async def fake_run(*_: object, **__: object) -> SSHResult:
        return next(replies)

    async def callback(line: str, _: int) -> None:
        observed.append(line)

    executor.run = fake_run
    original_sleep = asyncio.sleep
    asyncio.sleep = lambda _: original_sleep(0)
    try:
        assert (
            await executor.run_with_progress("host", "command", callback, 1)
        ).exit_code == 0
    finally:
        asyncio.sleep = original_sleep
    progress = [event for event in events if event.action.phase == "ssh_progress"]
    callback_events = [
        event for event in events if event.action.phase == "ssh_progress_callback"
    ]
    assert observed == ["progress"]
    assert progress[-1].lifecycle.state == LifecycleState.COMPLETED
    assert [event.lifecycle.state for event in callback_events] == [
        LifecycleState.REQUESTED,
        LifecycleState.COMPLETED,
    ]
    assert callback_events[0].parent_action_id == progress[0].action_id


@pytest.mark.parametrize("failure", ["output", "rc"])
async def test_progress_collection_failures_still_attempt_cleanup(failure: str) -> None:
    """Output and malformed-RC failures both remove the capture directory."""
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type("Recorder", (), {"record_critical": lambda *_: None})(),
    )
    replies = [
        SSHResult("/tmp/run\n", "", 0),
        SSHResult("__PID:9\n", "", 0),
        SSHResult("", "", 0),
    ]
    if failure == "output":
        replies.extend([SSHResult("", "cannot read", 255), SSHResult("", "", 0)])
    else:
        replies.extend(
            [
                SSHResult("output", "", 0),
                SSHResult("not-an-int", "", 0),
                SSHResult("", "", 0),
            ]
        )
    commands: list[str] = []

    async def fake_run(_host: str, command: str, **_: object) -> SSHResult:
        commands.append(command)
        return replies.pop(0)

    executor.run = fake_run
    original_sleep = asyncio.sleep
    asyncio.sleep = lambda _: original_sleep(0)
    try:
        result = await executor.run_with_progress("host", "command", poll_interval=1)
    finally:
        asyncio.sleep = original_sleep
    assert result.exit_code != 0
    assert "rm -rf /tmp/run" in commands


async def test_progress_post_launch_cancellation_attempts_cleanup() -> None:
    """Cancellation after PID capture has a cancelled parent and cleanup action."""
    events: list[object] = []
    executor = SSHExecutor(
        trace_context=new_trace_context(ticket_id="PERF-SSH"),
        trace_recorder=type(
            "Recorder", (), {"record_critical": lambda _, event: events.append(event)}
        )(),
    )
    replies = iter([SSHResult("/tmp/run\n", "", 0), SSHResult("__PID:9\n", "", 0)])
    commands: list[str] = []

    async def fake_run(_host: str, command: str, **_: object) -> SSHResult:
        commands.append(command)
        if command.startswith("rm -rf"):
            return SSHResult("", "", 0)
        return next(replies)

    async def cancelled(_: float) -> None:
        raise asyncio.CancelledError

    executor.run = fake_run
    original_sleep = asyncio.sleep
    asyncio.sleep = cancelled
    try:
        with pytest.raises(asyncio.CancelledError):
            await executor.run_with_progress("host", "command", poll_interval=1)
    finally:
        asyncio.sleep = original_sleep
    assert "rm -rf /tmp/run" in commands
    progress = [event for event in events if event.action.phase == "ssh_progress"]
    assert progress[-1].lifecycle.state == LifecycleState.CANCELLED


def test_audit_events_never_persist_command_stdin_or_stream_secrets(
    tmp_path: Path,
) -> None:
    """The durable fixture proves all three sensitive values stay out of traces."""
    secret = "audit-secret-must-not-persist"

    class Process:
        pid = 20
        returncode = 0

        async def communicate(self, input=None):
            return f"output {secret}".encode(), f"error {secret}".encode()

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        return Process()

    async def exercise() -> None:
        with TraceStore(tmp_path / "trace.db") as store:
            executor = SSHExecutor(
                trace_context=new_trace_context(ticket_id="PERF-SECRET"),
                trace_recorder=store,
            )
            original = asyncio.create_subprocess_exec
            asyncio.create_subprocess_exec = spawn
            try:
                await executor.run("host", f"echo {secret}", stdin_data=secret.encode())
            finally:
                asyncio.create_subprocess_exec = original

    asyncio.run(exercise())
    assert secret.encode() not in (tmp_path / "trace.db").read_bytes()


def test_requested_and_launched_actions_have_one_terminal() -> None:
    """The action lifecycle invariant holds for a concrete SSH operation."""
    events: list[object] = []

    class Process:
        pid = 21
        returncode = 0

        async def communicate(self, input=None):
            return b"", b""

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        return Process()

    async def exercise() -> None:
        executor = SSHExecutor(
            trace_context=new_trace_context(ticket_id="PERF-INVARIANT"),
            trace_recorder=type(
                "Recorder",
                (),
                {"record_critical": lambda _, event: events.append(event)},
            )(),
        )
        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = spawn
        try:
            await executor.run("host", "command")
        finally:
            asyncio.create_subprocess_exec = original

    asyncio.run(exercise())
    terminal = {
        LifecycleState.COMPLETED,
        LifecycleState.FAILED,
        LifecycleState.TIMED_OUT,
        LifecycleState.CANCELLED,
    }
    actions: dict[str, list[object]] = {}
    for event in events:
        actions.setdefault(event.action_id, []).append(event)
    for lifecycle in actions.values():
        if any(
            event.lifecycle.state in {LifecycleState.REQUESTED, LifecycleState.LAUNCHED}
            for event in lifecycle
        ):
            assert sum(event.lifecycle.state in terminal for event in lifecycle) == 1
