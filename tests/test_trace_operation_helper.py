"""Async no-replay and cancellation contract for operation helper."""

from __future__ import annotations

import asyncio

import pytest

from providers.tracing.operations import (
    AmbiguousOperation,
    OperationCancelled,
    operation,
)


def callbacks(fail: str | None = None):
    calls: list[tuple[str, object]] = []

    async def prepare(_: dict):
        calls.append(("prepare", None))
        if fail == "prepare":
            raise RuntimeError("secret-message")

    async def started(_: dict):
        calls.append(("started", None))
        if fail == "started":
            raise RuntimeError("secret-message")

    async def launch():
        calls.append(("launch", None))
        if fail == "launch":
            raise RuntimeError("secret-message")
        return "secret-result"

    async def complete(_, descriptor):
        calls.append(("complete", descriptor))
        if fail == "complete":
            raise RuntimeError("secret-message")

    async def indeterminate(_, descriptor):
        calls.append(("indeterminate", descriptor))

    return calls, prepare, started, launch, complete, indeterminate


async def test_terminal_and_existing_never_call_launch() -> None:
    calls, p, s, launch, c, i = callbacks()

    async def terminal():
        return {
            "status": "terminal",
            "operation": {"result_descriptor": {"id": "cached"}},
        }

    assert await operation(terminal, p, s, launch, c, i, lambda _: {}) == {
        "id": "cached"
    }
    assert calls == []

    async def existing():
        return {"status": "existing", "operation": {}}

    with pytest.raises(AmbiguousOperation):
        await operation(existing, p, s, launch, c, i, lambda _: {})
    assert calls == []


@pytest.mark.parametrize(
    "phase, expected, error",
    [
        ("prepare", "complete", RuntimeError),
        ("started", "indeterminate", AmbiguousOperation),
        ("launch", "indeterminate", AmbiguousOperation),
        ("complete", "indeterminate", AmbiguousOperation),
    ],
)
async def test_failures_are_durable_and_redacted(
    phase: str, expected: str, error: type[Exception]
) -> None:
    calls, p, s, launch, c, i = callbacks(phase)

    async def acquire():
        return {"status": "acquired", "operation": {"id": "lease"}}

    with pytest.raises(error):
        await operation(acquire, p, s, launch, c, i, lambda _: {"outcome": "success"})
    selected = [data for name, data in calls if name == expected]
    assert selected and "secret-message" not in str(selected[-1])
    assert sum(name == "launch" for name, _ in calls) == (
        0 if phase in {"prepare", "started"} else 1
    )


async def test_acquire_cancellation_has_no_durable_callback() -> None:
    calls, p, s, launch, c, i = callbacks()

    async def acquire():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await operation(acquire, p, s, launch, c, i, lambda _: {})
    assert calls == []


@pytest.mark.parametrize("phase", ["started", "launch", "complete"])
async def test_post_boundary_cancellation_is_indeterminate(phase: str) -> None:
    calls, p, s, launch, c, i = callbacks()

    async def acquire():
        return {"status": "acquired", "operation": {}}

    async def cancel_started(_):
        raise asyncio.CancelledError()

    async def cancel_launch():
        raise asyncio.CancelledError()

    async def cancel_complete(_, __):
        raise asyncio.CancelledError()

    with pytest.raises(OperationCancelled):
        await operation(
            acquire,
            p,
            cancel_started if phase == "started" else s,
            cancel_launch if phase == "launch" else launch,
            cancel_complete if phase == "complete" else c,
            i,
            lambda _: {"safe": True},
        )
    assert any(name == "indeterminate" for name, _ in calls)


async def test_success_uses_safe_descriptor_not_raw_result() -> None:
    calls, p, s, launch, c, i = callbacks()

    async def acquire():
        return {"status": "acquired", "operation": {}}

    assert (
        await operation(
            acquire, p, s, launch, c, i, lambda _: {"outcome": "success", "id": "safe"}
        )
        == "secret-result"
    )
    assert [data for name, data in calls if name == "complete"] == [
        {"outcome": "success", "id": "safe"}
    ]


async def test_prepare_cancellation_is_completed_without_launch() -> None:
    calls, _, s, launch, c, i = callbacks()

    async def acquire():
        return {"status": "acquired", "operation": {}}

    async def cancelled(_):
        raise asyncio.CancelledError()

    with pytest.raises(OperationCancelled):
        await operation(acquire, cancelled, s, launch, c, i, lambda _: {})
    assert [name for name, _ in calls] == ["complete"]
    assert calls[0][1]["outcome"] == "cancelled"
