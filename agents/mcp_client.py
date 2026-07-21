from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from providers.llm.base import ToolDefinition

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
        # Optional hook for provider-specific call_tool
        # behavior (e.g., Jumpstarter connect guards).
        # Signature: async (name, arguments) -> str | None
        # Return a string to short-circuit; None to proceed.
        self.pre_call_hook: Any = None
        # Optional hook for post-processing tool results.
        # Signature: (name, content) -> str
        self.post_call_hook: Any = None

    async def connect(
        self,
        server_script: str,
        name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Connect to a Python MCP server script.

        Launches the script with the current Python interpreter.
        For non-Python MCP servers (e.g., Jumpstarter's
        ``jmp mcp serve``), use connect_command() instead.
        """
        await self.connect_command(
            command=sys.executable,
            args=[server_script],
            name=name or server_script,
            env=env,
        )

    async def connect_command(
        self,
        command: str,
        args: list[str] | None = None,
        name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Connect to an MCP server started by an arbitrary command.

        This supports non-Python MCP servers such as Jumpstarter
        (``jmp mcp serve``) or any other binary that speaks MCP
        over stdio. The underlying transport is identical to
        connect() — only the launch command differs.

        Args:
            command: The executable to run (e.g., "jmp").
            args: Arguments to pass (e.g., ["mcp", "serve"]).
            name: Display name for logging and tool routing.
            env: Extra environment variables (merged with
                os.environ).
        """
        if name is None:
            name = command

        project_root = str(Path(__file__).resolve().parent.parent)
        base_env = {**os.environ}
        existing = base_env.get("PYTHONPATH", "")
        if project_root not in existing.split(os.pathsep):
            base_env["PYTHONPATH"] = (
                f"{project_root}{os.pathsep}{existing}" if existing else project_root
            )
        merged_env = {**base_env, **(env or {})}

        params = StdioServerParameters(
            command=command,
            args=args or [],
            env=merged_env,
        )
        stdio_cm = stdio_client(params)
        session_cm = None
        try:
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
        except (Exception, BaseException):
            # Clean up in the same task that entered the
            # context managers. Prevents anyio cancel scope
            # mismatches (modelcontextprotocol/python-sdk#577)
            # when the agent task is cancelled during setup.
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except (Exception, BaseException):
                    pass
            try:
                await stdio_cm.__aexit__(None, None, None)
            except (Exception, BaseException):
                pass
            raise

    async def list_tools(
        self,
        include: set[str] | None = None,
    ) -> list[ToolDefinition]:
        """List tools from all connected servers.

        Args:
            include: If provided, only return tools whose names
                are in this set. Tools not in the set are still
                callable via call_tool() — this only controls
                what the LLM sees. If None, all tools are
                returned.
        """
        tools = []
        for conn in self._servers.values():
            result = await conn.session.list_tools()
            for t in result.tools:
                if include is not None and t.name not in include:
                    continue
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

        # Pre-call hook: provider-specific guards
        # (e.g., Jumpstarter one-connect, timeout).
        if self.pre_call_hook is not None:
            short_circuit = await self.pre_call_hook(name, arguments)
            if short_circuit is not None:
                return short_circuit

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

        # Post-call hook: provider-specific response
        # trimming (e.g., Jumpstarter verbose output).
        if self.post_call_hook is not None:
            content = self.post_call_hook(name, content)

        return content

    async def disconnect(self) -> None:
        for conn in list(self._servers.values()):
            # Close session and transport. Catches all
            # exceptions including RuntimeError from
            # anyio cancel scope mismatches (MCP SDK
            # python-sdk#577). This occurs when the
            # agent task is cancelled externally and
            # cleanup runs from a different task context.
            # The connection is already dead at that
            # point — logging and moving on is correct.
            for cm_name, cm in [
                ("session", conn.session_cm),
                ("transport", conn.stdio_cm),
            ]:
                try:
                    await cm.__aexit__(None, None, None)
                except (Exception, BaseException) as exc:
                    logger.debug(
                        "Error closing %s for %s: %s",
                        cm_name,
                        conn.name,
                        type(exc).__name__,
                    )
        self._servers.clear()
        self._tool_routing.clear()
        logger.info("MCP client disconnected all servers")
