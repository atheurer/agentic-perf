"""Regression coverage for bounded, correlated SSH trace metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from providers.ssh import SSHExecutor
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
