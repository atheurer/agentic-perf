"""Behavioral contract for the shared audited subprocess runner."""

from __future__ import annotations

import asyncio
import sys

import pytest

from providers.execution.subprocess import AuditedSubprocessRunner
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


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
