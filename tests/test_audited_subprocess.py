"""Behavioral contract for the shared audited subprocess runner."""

from __future__ import annotations

import asyncio
import hashlib
import sys

import pytest

from providers.execution.subprocess import AuditedSubprocessRunner
from providers.tracing import (
    LifecycleState,
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)
from providers.tracing.client import TraceDeliveryError


async def test_success_nonzero_timeout_and_bounded_output() -> None:
    runner = AuditedSubprocessRunner(output_limit=3)
    success = await runner.run([sys.executable, "-c", "print('hello')"])
    assert success.outcome == "success" and success.stdout == b"hel"
    failed = await runner.run([sys.executable, "-c", "import sys;sys.exit(3)"])
    assert failed.returncode == 3 and failed.outcome == "failure"
    timed_out = await runner.run(
        [sys.executable, "-c", "import time;time.sleep(1)"], timeout=0.01
    )
    assert timed_out.timed_out and timed_out.outcome == "timed_out"


async def test_ticket_context_emits_start_and_one_terminal() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        result = await AuditedSubprocessRunner(emit).run([sys.executable, "-c", "pass"])
    finally:
        reset_trace_context(token)
    assert result.pid and [event.lifecycle.state.value for event in events] == [
        "requested",
        "started",
        "completed",
    ]


async def test_timeout_and_cancellation_emit_one_correct_terminal() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        runner = AuditedSubprocessRunner(emit)
        await runner.run(
            [sys.executable, "-c", "import time;time.sleep(1)"], timeout=0.01
        )
        assert [event.lifecycle.state.value for event in events] == [
            "requested",
            "started",
            "timed_out",
        ]
        assert events[-1].attributes["stdout_size"] == 0
        assert events[-1].attributes["stderr_truncated"] is False
        events.clear()
        task = asyncio.create_task(
            runner.run([sys.executable, "-c", "import time;time.sleep(1)"])
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [event.lifecycle.state.value for event in events] == [
            "requested",
            "started",
            "cancelled",
        ]
    finally:
        reset_trace_context(token)


async def test_tracked_communicate_timeout_escalates_and_is_timed_out() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        process = await AuditedSubprocessRunner(emit, shutdown_timeout=0.1).start(
            [
                sys.executable,
                "-c",
                "import signal,time;signal.signal(signal.SIGTERM, lambda *_: None);print('ready', flush=True);time.sleep(5)",
            ]
        )
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
        assert ready == b"ready\n"
        with pytest.raises(asyncio.TimeoutError):
            await process.communicate(timeout=0.01)
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "started",
        "timed_out",
    ]
    assert events[-1].attributes["signal"] == "kill"


async def test_secret_argv_env_and_stdin_are_not_in_events() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        result = await AuditedSubprocessRunner(emit, output_limit=3).run(
            [sys.executable, "-c", "print('abcdef')", "secret-argv"],
            env={"SECRET": "secret-env"},
            stdin=b"secret-stdin",
        )
    finally:
        reset_trace_context(token)
    assert result.stdout == b"abc"
    assert all("secret" not in str(event.attributes) for event in events)


async def test_repeated_wait_emits_one_terminal_and_binary_output_is_bounded() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        runner = AuditedSubprocessRunner(emit, output_limit=2)
        process = await runner.start(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xffabc')"]
        )
        await process.wait()
        await process.wait()
        result = await runner.run(
            [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xffabc')"]
        )
    finally:
        reset_trace_context(token)
    assert result.stdout == b"\xffa"[:2]
    assert [event.lifecycle.state.value for event in events[:3]] == [
        "requested",
        "started",
        "completed",
    ]


async def test_streamed_output_preserves_terminal_metadata_and_incomplete_failure() -> (
    None
):
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        runner = AuditedSubprocessRunner(emit, output_limit=3)
        for complete in (True, False):
            process = await runner.start(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write(b'abcdef');sys.stderr.buffer.write(b'err!')",
                ]
            )
            stdout, stderr = await process._process.communicate()
            await process.finish_streamed_output(
                stdout, stderr, drain_complete=complete
            )
            await process.finish_streamed_output(
                stdout, stderr, drain_complete=complete
            )
            terminal = events[-1]
            assert terminal.lifecycle.state.value == (
                "completed" if complete else "failed"
            )
            assert terminal.attributes["stdout_size"] == 6
            assert terminal.attributes["stderr_size"] == 4
            assert (
                terminal.attributes["stdout_digest"]
                == hashlib.sha256(stdout).hexdigest()[:16]
            )
            assert (
                terminal.attributes["stderr_digest"]
                == hashlib.sha256(stderr).hexdigest()[:16]
            )
            assert terminal.attributes["stdout_truncated"] is True
            assert terminal.attributes["stderr_truncated"] is True
            assert terminal.attributes["output_incomplete"] is not complete
        assert [event.lifecycle.state.value for event in events] == [
            "requested",
            "started",
            "completed",
            "requested",
            "started",
            "failed",
        ]
    finally:
        reset_trace_context(token)


async def test_streamed_output_timeout_and_cancellation_emit_one_terminal() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        runner = AuditedSubprocessRunner(emit)
        for state, options in (
            ("timed_out", {"timed_out": True}),
            ("cancelled", {"cancelled": True}),
        ):
            process = await runner.start([sys.executable, "-c", "pass"])
            await process._process.communicate()
            for _ in range(2):
                await process.finish_streamed_output(
                    b"partial", b"", drain_complete=False, **options
                )
            terminal = events[-1]
            assert terminal.lifecycle.state.value == state
            assert terminal.attributes["stdout_size"] == len(b"partial")
            assert terminal.attributes["output_incomplete"] is True
            assert terminal.attributes.get("timed_out", False) is (state == "timed_out")
        assert [event.lifecycle.state.value for event in events] == [
            "requested",
            "started",
            "timed_out",
            "requested",
            "started",
            "cancelled",
        ]
    finally:
        reset_trace_context(token)


async def test_mutating_spawn_requires_critical_recorder() -> None:
    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        with pytest.raises(TraceDeliveryError):
            await AuditedSubprocessRunner().run(
                [sys.executable, "-c", "pass"], mutating=True
            )
    finally:
        reset_trace_context(token)


async def test_default_recorder_is_shared_and_reset_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from providers.execution import subprocess as subprocess_module

    AuditedSubprocessRunner.reset_default_recorder()

    instances = []

    class FakeTraceClient:
        def __init__(self, url: str, token: str) -> None:
            self.url = url
            self.token = token
            self.closed = False
            instances.append(self)

        def record(self, _event) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("STATE_STORE_URL", "http://state-store.invalid")
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")
    monkeypatch.setattr(subprocess_module, "TraceClient", FakeTraceClient)

    first = AuditedSubprocessRunner()
    second = AuditedSubprocessRunner()
    event = first._event(
        LifecycleState.REQUESTED,
        ["demo"],
        context=new_trace_context(ticket_id="PERF-1"),
    )
    assert event is not None

    try:
        await first._record(event)
        await second._record(event)

        assert len(instances) == 1
        assert first._recorder is instances[0]
        assert second._recorder is instances[0]
        assert AuditedSubprocessRunner._default_recorder is instances[0]

        AuditedSubprocessRunner.reset_default_recorder()
        assert instances[0].closed
        assert AuditedSubprocessRunner._default_recorder is None

        third = AuditedSubprocessRunner()
        await third._record(event)
        assert len(instances) == 2
        assert third._recorder is instances[1]
        assert instances[1] is not instances[0]
    finally:
        AuditedSubprocessRunner.reset_default_recorder()


async def test_mutating_spawn_without_context_fails_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("mutating command must fail before spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_spawn)
    with pytest.raises(TraceDeliveryError, match="ticket trace context"):
        await AuditedSubprocessRunner().run(
            [sys.executable, "-c", "pass"], mutating=True
        )


async def test_spawn_error_is_audited_on_child_action() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        with pytest.raises(FileNotFoundError):
            await AuditedSubprocessRunner(emit).run(["definitely-not-a-command"])
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state.value for event in events] == ["requested", "failed"]
    assert events[0].action_id == events[1].action_id
    assert events[0].parent_action_id is not None


async def test_signal_then_wait_has_one_terminal() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        process = await AuditedSubprocessRunner(emit).start(
            [sys.executable, "-c", "import time;time.sleep(1)"]
        )
        process.terminate()
        await process.wait()
        await process.wait()
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "started",
        "failed",
    ]
    assert events[-1].attributes["signal"] == "terminate"


async def test_stdin_failure_finishes_the_started_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure must not leave a durable STARTED action open."""

    events = []

    class BrokenStdin:
        def write(self, _data: bytes) -> None:
            raise BrokenPipeError

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Process:
        pid = 4242
        returncode = -15
        stdin = BrokenStdin()

        def terminate(self) -> None:
            return None

        async def wait(self) -> int:
            return self.returncode

    async def spawn(*_args, **_kwargs):
        return Process()

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    token = bind_trace_context(new_trace_context(ticket_id="PERF-1"))
    try:
        with pytest.raises(BrokenPipeError):
            await AuditedSubprocessRunner(emit).start(["demo"], stdin=b"input")
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "started",
        "failed",
    ]
    assert events[-1].attributes["stdin_error"] is True
