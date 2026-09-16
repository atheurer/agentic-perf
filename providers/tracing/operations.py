"""Cancellation-safe single-launch helper for fenced operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class OperationCancelled(RuntimeError):
    """Cancellation occurred after a durable operation boundary."""


class AmbiguousOperation(RuntimeError):
    """The side effect may have started; reconcile instead of replaying it."""


async def _durable(callback: Callable[..., Awaitable[None]], *args: Any) -> None:
    """Finish state recording despite caller cancellation."""
    task = asyncio.create_task(callback(*args))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


def _safe_error(exc: BaseException, outcome: str) -> dict[str, str]:
    """Exception messages may carry secrets, so never persist them."""
    return {"outcome": outcome, "type": type(exc).__name__}


async def operation(
    acquire: Callable[[], Awaitable[dict[str, Any]]],
    prepare: Callable[[dict[str, Any]], Awaitable[None]],
    mark_started: Callable[[dict[str, Any]], Awaitable[None]],
    launch: Callable[[], Awaitable[Any]],
    complete: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]],
    indeterminate: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]],
    success_descriptor: Callable[[Any], dict[str, Any]],
) -> Any:
    """Launch only ``acquired`` operations; terminal/existing never replay."""
    acquisition = await acquire()
    status = acquisition.get("status")
    if status == "terminal":
        return acquisition.get("operation", {}).get("result_descriptor")
    if status != "acquired":
        raise AmbiguousOperation("operation is already owned; do not replay")
    lease = acquisition["operation"]
    try:
        await prepare(lease)
    except asyncio.CancelledError as exc:
        await _durable(complete, lease, _safe_error(exc, "cancelled"))
        raise OperationCancelled("cancelled before side effect") from exc
    except Exception as exc:
        await _durable(complete, lease, _safe_error(exc, "failure"))
        raise
    try:
        # This boundary is deliberately before await: an acknowledgement may be lost.
        await mark_started(lease)
        result = await launch()
        await _durable(complete, lease, success_descriptor(result))
        return result
    except asyncio.CancelledError as exc:
        await _durable(indeterminate, lease, _safe_error(exc, "cancelled"))
        raise OperationCancelled("cancelled after side-effect boundary") from exc
    except Exception as exc:
        await _durable(indeterminate, lease, _safe_error(exc, "indeterminate"))
        raise AmbiguousOperation("side effect may have completed") from exc
