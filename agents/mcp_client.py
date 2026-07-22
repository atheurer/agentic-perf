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
    transport_cm: Any  # stdio_client, sse_client, or streamablehttp_client
    session_cm: Any


class AgentMCPClient:
    """MCP client that connects to one or more MCP servers.

    Supports three transport modes:
    - stdio: connect() / connect_command() for subprocess servers
    - SSE: connect_sse() for remote servers via Server-Sent Events
    - StreamableHTTP: connect_streamable_http() for remote servers
      via HTTP with streaming

    Call any connect method once per server. list_tools() merges
    tools from all servers. call_tool() routes to the server that
    provides the tool. Tool name conflicts across servers raise
    ValueError at connect time.
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
        transport_cm = stdio_client(params)
        await self._connect_transport(name, transport_cm)

    async def connect_sse(
        self,
        url: str,
        name: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        sse_read_timeout: float = 300,
        trust: bool = False,
    ) -> None:
        """Connect to a remote MCP server via SSE transport.

        The server must expose an SSE endpoint (typically at
        /sse or /mcp). The client maintains a persistent
        connection for server-to-client messages.

        Args:
            url: SSE endpoint URL (e.g.,
                "http://domain-mcp.lab:8080/mcp").
            name: Display name for logging and tool routing.
            headers: HTTP headers (e.g., Authorization).
            timeout: Connection timeout in seconds.
            sse_read_timeout: Read timeout for SSE stream.
            trust: If True, disable SSL certificate
                verification (for self-signed certs).
        """
        from mcp.client.sse import sse_client

        if name is None:
            name = url

        kwargs: dict[str, Any] = {
            "url": url,
            "headers": headers,
            "timeout": timeout,
            "sse_read_timeout": sse_read_timeout,
        }
        if trust:
            import httpx

            def _insecure_factory(*args: Any, **kw: Any) -> httpx.AsyncClient:
                return httpx.AsyncClient(verify=False, *args, **kw)  # nosec B501 — user explicitly set trust=True

            kwargs["httpx_client_factory"] = _insecure_factory

        transport_cm = sse_client(**kwargs)
        await self._connect_transport(name, transport_cm)

    async def connect_streamable_http(
        self,
        url: str,
        name: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        sse_read_timeout: float = 300,
        trust: bool = False,
    ) -> None:
        """Connect to a remote MCP server via StreamableHTTP.

        The server must expose an MCP endpoint that supports
        the StreamableHTTP protocol (typically at /mcp/http).

        Args:
            url: MCP endpoint URL (e.g.,
                "http://domain-mcp.lab:8080/mcp/http").
            name: Display name for logging and tool routing.
            headers: HTTP headers (e.g., Authorization).
            timeout: Request timeout in seconds.
            sse_read_timeout: Read timeout for streaming.
            trust: If True, disable SSL certificate
                verification (for self-signed certs).
        """
        from mcp.client.streamable_http import (
            streamablehttp_client,
        )

        if name is None:
            name = url

        kwargs: dict[str, Any] = {
            "url": url,
            "headers": headers,
            "timeout": timeout,
            "sse_read_timeout": sse_read_timeout,
        }
        if trust:
            import httpx

            def _insecure_factory(*args: Any, **kw: Any) -> httpx.AsyncClient:
                return httpx.AsyncClient(verify=False, *args, **kw)  # nosec B501 — user explicitly set trust=True

            kwargs["httpx_client_factory"] = _insecure_factory

        transport_cm = streamablehttp_client(**kwargs)
        await self._connect_transport(name, transport_cm)

    async def _connect_transport(
        self,
        name: str,
        transport_cm: Any,
    ) -> None:
        """Shared connection logic for all transports.

        Enters the transport context manager, creates a
        ClientSession, initializes it, registers tools,
        and stores the connection.

        If any step fails (error, cancellation), cleans
        up partially-entered context managers in the same
        task to avoid anyio cancel scope mismatches
        (modelcontextprotocol/python-sdk#577).
        """
        session_cm = None
        try:
            streams = await transport_cm.__aenter__()
            # SSE yields (read, write), StreamableHTTP
            # yields (read, write, get_session_id).
            read_stream = streams[0]
            write_stream = streams[1]
            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()
            await session.initialize()

            result = await session.list_tools()
            for t in result.tools:
                if t.name in self._tool_routing:
                    existing_server = self._tool_routing[t.name]
                    raise ValueError(
                        f"Tool {t.name!r} from server "
                        f"{name!r} conflicts with server "
                        f"{existing_server!r}"
                    )
                self._tool_routing[t.name] = name

            self._servers[name] = _ServerConnection(
                name=name,
                session=session,
                transport_cm=transport_cm,
                session_cm=session_cm,
            )
            logger.info(
                "MCP client connected to %s (%d tools)",
                name,
                len(result.tools),
            )
        except (Exception, BaseException):
            # Clean up in the same task that entered
            # the context managers.
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except (Exception, BaseException):
                    pass
            try:
                await transport_cm.__aexit__(None, None, None)
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
            for cm_name, cm in [
                ("session", conn.session_cm),
                ("transport", conn.transport_cm),
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


async def connect_external_servers(
    client: AgentMCPClient,
    agent_type: str,
    config: dict[str, Any] | None = None,
    secrets_dir: str = "",
) -> tuple[list[str], set[str] | None]:
    """Connect an MCP client to external servers configured
    for the given agent type.

    Reads ``external_mcp_servers`` from config and connects
    to each server whose ``agents`` dict includes the given
    agent_type. Returns the list of server names connected
    and a set of enabled tool names (or None for all tools).

    The ``agents`` field is a dict mapping agent type keys to
    their configuration::

        "agents": {
            "gathering_context": {
                "enabled_tools": "all"
            },
            "review": {
                "enabled_tools": [
                    "get_baseline_stats",
                    "compare_run_to_baseline"
                ]
            }
        }

    ``enabled_tools`` controls which tools from this server
    the agent's LLM can see:

    - ``"all"`` or omitted: all tools visible
    - list of names: only those tools visible

    Tools not in ``enabled_tools`` are hidden from the LLM
    but remain callable via ``call_tool()`` in code.

    Args:
        client: The agent's MCP client.
        agent_type: Agent type key (e.g., "gathering_context").
        config: Config dict. If None, reads from config file.
        secrets_dir: Base directory for secrets files.
            Defaults to ~/.agentic-perf/secrets/.

    Returns:
        Tuple of (connected server names, enabled tool names).
        The tool set is None if all tools are enabled, or a
        set of tool name strings if filtering is configured.

    Example:
        .. code-block:: python

            mcp = AgentMCPClient()
            await mcp.connect(agent_server, name="agent")
            connected, enabled = await connect_external_servers(
                mcp, "gathering_context"
            )
            # Filter tools for LLM visibility
            if enabled is not None:
                self.tools = [
                    t for t in self.tools
                    if t.name in enabled
                ]
    """
    from pathlib import Path

    if config is None:
        import json

        config_path = (
            Path(
                os.environ.get(
                    "AGENTIC_PERF_HOME",
                    str(Path.home() / ".agentic-perf"),
                )
            )
            / "config.json"
        )
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}

    if not secrets_dir:
        secrets_dir = str(Path.home() / ".agentic-perf" / "secrets")

    servers = config.get("external_mcp_servers", [])
    connected: list[str] = []
    # Collect enabled tools across all connected servers.
    # None means "all tools" (no filtering). A set means
    # only those tools are visible to the LLM.
    enabled_tools: set[str] | None = None
    _has_scoping = False

    for entry in servers:
        name = entry.get("name", "")
        url = entry.get("url", "")
        transport = entry.get("transport", "")
        agents = entry.get("agents", {})

        # agents is a dict mapping agent types to config.
        # Skip if this agent type isn't listed.
        if isinstance(agents, dict):
            if agent_type not in agents:
                continue
            agent_config = agents[agent_type]
            if not isinstance(agent_config, dict):
                agent_config = {}
        elif isinstance(agents, list):
            # Legacy list format — all tools enabled.
            if agents and agent_type not in agents:
                continue
            agent_config = {}
        else:
            continue

        if not url or not transport:
            logger.warning(
                f"[mcp] Skipping external server {name!r}: missing url or transport"
            )
            continue

        # Resolve auth token from secrets
        headers: dict[str, str] = {}
        secret_path = entry.get("secret", "")
        if secret_path:
            token_file = Path(secrets_dir) / secret_path
            if token_file.exists():
                token = token_file.read_text().strip()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            else:
                logger.warning(
                    f"[mcp] Secret {secret_path} not found for server {name!r}"
                )

        try:
            trust = entry.get("trust", False)

            if transport == "sse":
                await client.connect_sse(
                    url=url,
                    name=name,
                    headers=headers or None,
                    trust=trust,
                )
            elif transport == "streamable_http":
                await client.connect_streamable_http(
                    url=url,
                    name=name,
                    headers=headers or None,
                    trust=trust,
                )
            else:
                logger.warning(
                    f"[mcp] Unknown transport {transport!r} for server {name!r}"
                )
                continue

            connected.append(name)
            logger.info(f"[mcp] Connected to external server {name!r} ({transport})")

            # Collect tool scoping for this agent.
            tools_cfg = agent_config.get("enabled_tools", "all")
            if isinstance(tools_cfg, list):
                _has_scoping = True
                if enabled_tools is None:
                    enabled_tools = set(tools_cfg)
                else:
                    enabled_tools.update(tools_cfg)
            # "all" or omitted — no filtering for
            # this server (but other servers may
            # still add scoping).
        except Exception:
            logger.warning(
                f"[mcp] Failed to connect to {name!r} at {url}",
                exc_info=True,
            )

    # If any server specified a tool list, return the
    # union. If all servers used "all", return None.
    if not _has_scoping:
        enabled_tools = None
    return connected, enabled_tools
