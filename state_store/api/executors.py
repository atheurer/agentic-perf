"""Reserved worker paths for state-store liveness and control operations."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from fastapi import HTTPException, Request


class BoundedExecutorSaturated(RuntimeError):
    """The executor has no free running-or-queued admission slot."""


class BoundedThreadPoolExecutor:
    """Track admitted work and support a deadline-bounded shutdown."""

    def __init__(
        self,
        *,
        max_workers: int,
        queue_size: int,
        thread_name_prefix: str,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self.slots = threading.BoundedSemaphore(max_workers + queue_size)
        self._condition = threading.Condition()
        self._futures: set[Future] = set()

    def submit(self, function, /, *args, **kwargs) -> Future:
        future = self._executor.submit(function, *args, **kwargs)
        with self._condition:
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future

    def _discard_future(self, future: Future) -> None:
        with self._condition:
            self._futures.discard(future)
            self._condition.notify_all()

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


async def run_bounded_executor(
    executor,
    slots,
    function,
    /,
    *args,
    **kwargs,
):
    """Run work within admission and retain the slot until the thread finishes."""
    if slots is not None and not slots.acquire(blocking=False):
        raise BoundedExecutorSaturated
    context = contextvars.copy_context()
    try:
        future = executor.submit(context.run, function, *args, **kwargs)
    except BaseException:
        if slots is not None:
            slots.release()
        raise
    if slots is not None:
        future.add_done_callback(lambda _future: slots.release())
    return await asyncio.wrap_future(future)


async def run_control_worker(request: Request, function, /, *args, **kwargs):
    """Run a bounded control-plane read without sharing trace-query workers."""
    executor = getattr(request.app.state, "control_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    slots = getattr(request.app.state, "control_slots", None)
    try:
        return await run_bounded_executor(executor, slots, function, *args, **kwargs)
    except BoundedExecutorSaturated as exc:
        raise HTTPException(
            status_code=503, detail="control worker queue is full"
        ) from exc


async def run_lease_worker(request: Request, function, /, *args, **kwargs):
    """Run a lease operation without sharing health or trace-query workers."""
    executor = getattr(request.app.state, "lease_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    slots = getattr(request.app.state, "lease_slots", None)
    try:
        return await run_bounded_executor(executor, slots, function, *args, **kwargs)
    except BoundedExecutorSaturated as exc:
        raise HTTPException(
            status_code=503, detail="lease worker queue is full"
        ) from exc
