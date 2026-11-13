"""Reserved worker paths for state-store liveness and control operations."""

from __future__ import annotations

import asyncio
import contextvars
from functools import partial

from fastapi import Request


async def run_control_worker(request: Request, function, /, *args, **kwargs):
    """Run a bounded control-plane read without sharing trace-query workers."""
    executor = getattr(request.app.state, "control_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))


async def run_lease_worker(request: Request, function, /, *args, **kwargs):
    """Run a lease operation without sharing health or trace-query workers."""
    executor = getattr(request.app.state, "lease_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    return await loop.run_in_executor(
        executor, partial(context.run, function, *args, **kwargs)
    )
