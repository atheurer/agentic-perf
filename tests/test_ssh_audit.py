"""Regression coverage for bounded, correlated SSH trace metadata."""

from __future__ import annotations

from pathlib import Path

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
