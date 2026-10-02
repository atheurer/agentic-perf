"""Tests for the agent-owned stdio MCP transport."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from agents.mcp_stdio import audited_stdio_client


@pytest.mark.asyncio
async def test_stdout_noise_is_ignored_and_bad_jsonrpc_is_forwarded(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ignore startup text but preserve malformed protocol errors."""
    server = tmp_path / "noisy_server.py"
    server.write_text(
        textwrap.dedent(
            """\
            import sys

            sys.stdout.write("MCP startup banner\\n\\n")
            sys.stdout.write('{"level":"info","event":"started"}\\n')
            sys.stdout.write('{"jsonrpc":"2.0","id":1,\\n')
            sys.stdout.write(
                '{"jsonrpc":"2.0","method":"notifications/initialized"}\\n'
            )
            sys.stdout.flush()
            for _ in sys.stdin:
                pass
            """
        )
    )
    params = StdioServerParameters(command=sys.executable, args=[str(server)])
    processes = []
    caplog.set_level("DEBUG", logger="agents.mcp_stdio")

    async with audited_stdio_client(params, processes.append) as (read_stream, _):
        with anyio.fail_after(5):
            protocol_error = await read_stream.receive()
            valid_message = await read_stream.receive()

    assert len(processes) == 1
    assert isinstance(protocol_error, ValidationError)
    assert isinstance(valid_message, SessionMessage)
    assert valid_message.message.root.method == "notifications/initialized"
    assert "Ignoring non-JSONRPC line from server" in caplog.text
    assert "MCP startup banner" not in caplog.text
    assert '"level":"info"' not in caplog.text
