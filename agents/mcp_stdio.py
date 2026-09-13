"""Agent-owned stdio MCP transport with observable subprocess ownership.

Adapted from the pinned ``mcp~=1.28`` stdio client implementation.  The SDK
transport hides the child handle, and replacing its module-global process
factory is unsafe when two clients connect at once.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TextIO

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.abc import Process
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    StdioServerParameters,
    _get_executable_command,
    get_default_environment,
)
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.os.win32.utilities import (
    FallbackProcess,
    create_windows_process,
    terminate_windows_process_tree,
)
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)


async def _create_process(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
    errlog: TextIO,
    cwd: Any,
) -> Process | FallbackProcess:
    """Create an SDK-compatible child in an independently owned process group."""
    if sys.platform == "win32":  # pragma: no cover
        return await create_windows_process(command, args, env, errlog, cwd)
    return await anyio.open_process(
        [command, *args],
        env=env,
        stderr=errlog,
        cwd=cwd,
        start_new_session=True,
    )


async def _terminate_process_tree(process: Process | FallbackProcess) -> None:
    if sys.platform == "win32":  # pragma: no cover
        await terminate_windows_process_tree(process, PROCESS_TERMINATION_TIMEOUT)
    else:
        assert isinstance(process, Process)
        await terminate_posix_process_tree(process, PROCESS_TERMINATION_TIMEOUT)


@asynccontextmanager
async def audited_stdio_client(
    params: StdioServerParameters,
    on_process: Callable[[Process | FallbackProcess], None],
    *,
    errlog: TextIO = sys.stderr,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Spawn a stdio MCP server and expose its actual process to ``on_process``."""
    read_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_reader: MemoryObjectReceiveStream[SessionMessage]
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)

    try:
        process = await _create_process(
            _get_executable_command(params.command),
            params.args,
            (
                {**get_default_environment(), **params.env}
                if params.env is not None
                else get_default_environment()
            ),
            errlog,
            params.cwd,
        )
        on_process(process)
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_writer.aclose()
        await write_reader.aclose()
        raise

    async def stdout_reader() -> None:
        assert process.stdout, "Opened process is missing stdout"
        try:
            async with read_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=params.encoding,
                    errors=params.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:  # pragma: no cover
                            logger.exception(
                                "Failed to parse JSONRPC message from server"
                            )
                            await read_writer.send(exc)
                            continue
                        await read_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin, "Opened process is missing stdin"
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await process.stdin.send(
                        (payload + "\n").encode(
                            encoding=params.encoding,
                            errors=params.encoding_error_handler,
                        )
                    )
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group, process:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin:
                try:
                    await process.stdin.aclose()
                except Exception:  # pragma: no cover
                    pass
            try:
                with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                    await process.wait()
            except TimeoutError:
                await _terminate_process_tree(process)
            except ProcessLookupError:  # pragma: no cover
                pass
            await read_stream.aclose()
            await write_stream.aclose()
            await read_writer.aclose()
            await write_reader.aclose()
