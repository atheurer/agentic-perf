"""PID-observing wrapper around the MCP SDK's stdio transport lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.client import stdio as sdk_stdio


@asynccontextmanager
async def audited_stdio_client(
    params: Any, on_process: Callable[[int], None], factory: Any = None
) -> AsyncIterator[tuple[Any, Any]]:
    """Delegate SDK stdio semantics while observing its spawned child PID.

    The SDK deliberately hides the process handle from its public context
    manager. Its process factory is the single launch seam, so replacing it
    only for context entry preserves its stream, stderr, cancellation, and
    process-tree cleanup implementation.
    """
    if factory is not None and factory is not sdk_stdio.stdio_client:
        async with factory(params) as streams:
            yield streams
        return
    original = sdk_stdio._create_platform_compatible_process

    async def launch(*args: Any, **kwargs: Any) -> Any:
        process = await original(*args, **kwargs)
        on_process(process.pid)
        return process

    sdk_stdio._create_platform_compatible_process = launch
    try:
        async with sdk_stdio.stdio_client(params) as streams:
            yield streams
    finally:
        sdk_stdio._create_platform_compatible_process = original
