from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from providers.llm.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class _ServerConnection:
    name: str
    session: ClientSession
    stdio_cm: Any
    session_cm: Any


class AgentMCPClient:
    """MCP client that connects to one or more FastMCP servers over stdio.

    Call connect() once per server. list_tools() merges tools from all
    servers. call_tool() routes to the server that provides the tool.
    Tool name conflicts across servers raise ValueError at connect time.
    """

    def __init__(self) -> None:
        self._servers: dict[str, _ServerConnection] = {}
        self._tool_routing: dict[str, str] = {}

    async def connect(
        self,
        server_script: str,
        name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if name is None:
            name = server_script

        params = StdioServerParameters(
            command=sys.executable,
            args=[server_script],
            env=env,
        )
        stdio_cm = stdio_client(params)
        read_stream, write_stream = await stdio_cm.__aenter__()
        session_cm = ClientSession(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()

        result = await session.list_tools()
        for t in result.tools:
            if t.name in self._tool_routing:
                existing = self._tool_routing[t.name]
                raise ValueError(
                    f"Tool {t.name!r} from server {name!r} conflicts "
                    f"with server {existing!r}"
                )
            self._tool_routing[t.name] = name

        self._servers[name] = _ServerConnection(
            name=name,
            session=session,
            stdio_cm=stdio_cm,
            session_cm=session_cm,
        )
        logger.info(
            "MCP client connected to %s (%d tools)",
            name,
            len(result.tools),
        )

    async def list_tools(self) -> list[ToolDefinition]:
        tools = []
        for conn in self._servers.values():
            result = await conn.session.list_tools()
            for t in result.tools:
                tools.append(
                    ToolDefinition(
                        name=t.name,
                        description=t.description or "",
                        input_schema=t.inputSchema,
                    )
                )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        server_name = self._tool_routing.get(name)
        if server_name is None:
            raise RuntimeError(f"No server provides tool {name!r}")

        conn = self._servers[server_name]
        result = await conn.session.call_tool(name, arguments)
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        content = "\n".join(parts) if parts else ""
        if result.isError:
            raise RuntimeError(content)
        return content

    async def disconnect(self) -> None:
        for conn in list(self._servers.values()):
            try:
                await conn.session_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error closing session for %s", conn.name)
            try:
                await conn.stdio_cm.__aexit__(None, None, None)
            except (Exception, BaseException):
                logger.debug("Error closing stdio for %s", conn.name)
        self._servers.clear()
        self._tool_routing.clear()
        logger.info("MCP client disconnected all servers")
