from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agents.mcp_stdio import audited_stdio_client
from providers.llm.base import ToolDefinition
from providers.redaction import get_shared_redactor
from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    MCPIdentity,
    OperationOutcome,
    ProducerIdentity,
    TraceContext,
    TraceEventV1,
    current_trace_context,
)
from providers.tracing.client import TraceClient

logger = logging.getLogger(__name__)

# Retained solely as a unit-test injection seam.  Production stdio connections
# always use the agent-owned transport below, never the SDK's hidden-factory
# transport.
_SDK_STDIO_CLIENT = stdio_client


class MCPToolCallError(RuntimeError):
    """An MCP failure annotated with whether the request may have reached it."""

    def __init__(
        self,
        message: str,
        retry_classification: Literal[
            "validation",
            "intentional_agent_retry",
            "transport_before_send",
            "ambiguous_after_send",
        ],
    ) -> None:
        super().__init__(message)
        self.retry_classification = retry_classification


@dataclass
class _ServerConnection:
    name: str
    session: ClientSession | None
    transport: str = "unknown"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    reconnect_generation: int = 0
    endpoint: str | None = None
    client_process_identity: str | None = None
    subprocess_pid: int | None = None
    ticket_id: str | None = None
    agent_id: str | None = None
    subprocess_pid_capture: str = "not_applicable"
    _shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None


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

    def __init__(
        self,
        *,
        trace_client: TraceClient | None = None,
        audit_hook: Any = None,
    ) -> None:
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
        self.audit_events: list[TraceEventV1] = []
        self._audit_hook = audit_hook
        self._trace_client = trace_client
        if self._trace_client is None:
            token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
            url = os.environ.get("STATE_STORE_URL", "")
            if token and url:
                self._trace_client = TraceClient(url, token)

    async def connect(
        self,
        server_script: str,
        name: str | None = None,
        env: dict[str, str] | None = None,
        ticket_id: str | None = None,
        agent_id: str | None = None,
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
            ticket_id=ticket_id,
            agent_id=agent_id,
        )

    async def connect_ticket_server(
        self,
        server_script: str,
        *,
        name: str,
        ticket_id: str,
        state_store_url: str,
        agent_name: str,
    ) -> None:
        """Connect an agent-owned MCP server with required ticket identity.

        Generic and external MCP servers may use :meth:`connect`. Every local
        server participating in ticket execution must use this method so its
        workspace, state-store access, phase scoping, and audit attribution
        cannot silently lose caller identity.
        """
        required = {
            "TICKET_ID": ticket_id,
            "STATE_STORE_URL": state_store_url,
            "AGENT_NAME": agent_name,
        }
        missing = [key for key, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                "ticket-scoped MCP server requires non-empty " + ", ".join(missing)
            )
        await self.connect(
            server_script,
            name=name,
            env=required,
            ticket_id=ticket_id,
            agent_id=agent_name,
        )

    async def connect_command(
        self,
        command: str,
        args: list[str] | None = None,
        name: str | None = None,
        env: dict[str, str] | None = None,
        ticket_id: str | None = None,
        agent_id: str | None = None,
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
        process_holder: list[Any] = []
        transport_cm = (
            stdio_client(params)
            if stdio_client is not _SDK_STDIO_CLIENT
            else audited_stdio_client(params, process_holder.append)
        )
        await self._connect_transport(
            name,
            transport_cm,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            subprocess_process_holder=process_holder,
        )

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
        await self._connect_transport(name, transport_cm, transport="sse", endpoint=url)

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
        await self._connect_transport(
            name, transport_cm, transport="streamable_http", endpoint=url
        )

    async def _connect_transport(
        self,
        name: str,
        transport_cm: Any,
        *,
        transport: str,
        endpoint: str | None,
        ticket_id: str | None = None,
        agent_id: str | None = None,
        subprocess_process_holder: list[Any] | None = None,
    ) -> None:
        """Shared connection logic for all transports.

        Runs the transport and session context managers in a
        dedicated background task so their anyio cancel scopes
        stay isolated from the agent's main task. Without this,
        anyio's _deliver_cancellation retries via call_soon
        whenever the agent awaits asyncio.to_thread (LLM calls),
        burning 100% CPU on one core.
        """
        previous = self._servers.pop(name, None)
        generation = 0
        if previous is not None:
            generation = previous.reconnect_generation + 1
            self._record_boundary(previous, LifecycleState.DISCONNECTED)
            previous._shutdown.set()
            if previous._task is not None:
                previous._task.cancel()
            self._tool_routing = {
                tool: server
                for tool, server in self._tool_routing.items()
                if server != name
            }
        ready: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        shutdown = asyncio.Event()
        conn = _ServerConnection(
            name=name,
            session=None,
            transport=transport,
            session_id=uuid.uuid4().hex,
            reconnect_generation=generation,
            endpoint=endpoint,
            client_process_identity=f"pid:{os.getpid()}",
            ticket_id=ticket_id,
            agent_id=agent_id,
            subprocess_pid_capture=(
                "pending" if transport == "stdio" else "not_applicable"
            ),
            _shutdown=shutdown,
        )
        self._record_boundary(conn, LifecycleState.CONNECTING)

        async def _hold_connection() -> None:
            try:
                async with transport_cm as streams:
                    read_stream = streams[0]
                    write_stream = streams[1]
                    async with ClientSession(read_stream, write_stream) as session:
                        if subprocess_process_holder:
                            conn.subprocess_pid = subprocess_process_holder[0].pid
                            conn.subprocess_pid_capture = "captured"
                        self._record_boundary(
                            conn, LifecycleState.REQUEST_SENT, tool_name="initialize"
                        )
                        await session.initialize()
                        self._record_boundary(
                            conn,
                            LifecycleState.RESPONSE_RECEIVED,
                            tool_name="initialize",
                            outcome=OperationOutcome.SUCCESS,
                        )
                        ready.set_result(session)
                        await shutdown.wait()
            except (Exception, BaseException) as exc:
                if not ready.done():
                    ready.set_exception(exc)
                elif self._servers.get(name) is conn:
                    self._record_boundary(
                        conn,
                        LifecycleState.FAILED,
                        outcome=OperationOutcome.FAILURE,
                        tool_name="server_exit",
                    )
                raise

        task = asyncio.create_task(_hold_connection(), name=f"mcp:{name}")

        try:
            session = await ready
        except (Exception, BaseException):
            task.cancel()
            with contextlib.suppress(Exception, BaseException):
                await task
            raise

        conn.session = session
        self._servers[name] = conn
        self._record_boundary(conn, LifecycleState.CONNECTED)
        if generation:
            self._record_boundary(conn, LifecycleState.RECONNECTED)
        self._record_boundary(conn, LifecycleState.REQUEST_SENT, tool_name="list_tools")
        try:
            result = await session.list_tools()
        except asyncio.CancelledError:
            self._record_boundary(
                conn,
                LifecycleState.CANCELLED,
                tool_name="list_tools",
                outcome=OperationOutcome.CANCELLED,
            )
            raise
        except Exception:
            self._record_boundary(
                conn,
                LifecycleState.FAILED,
                tool_name="list_tools",
                outcome=OperationOutcome.FAILURE,
            )
            raise
        self._record_boundary(
            conn,
            LifecycleState.RESPONSE_RECEIVED,
            tool_name="list_tools",
            outcome=OperationOutcome.SUCCESS,
        )
        for t in result.tools:
            if t.name in self._tool_routing:
                existing_server = self._tool_routing[t.name]
                shutdown.set()
                task.cancel()
                with contextlib.suppress(Exception, BaseException):
                    await task
                raise ValueError(
                    f"Tool {t.name!r} from server "
                    f"{name!r} conflicts with server "
                    f"{existing_server!r}"
                )
            self._tool_routing[t.name] = name

        conn._task = task
        logger.info(
            "MCP client connected to %s (%d tools)",
            name,
            len(result.tools),
        )

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

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        trace_context: TraceContext | None = None,
    ) -> str:
        server_name = self._tool_routing.get(name)
        if server_name is None:
            raise MCPToolCallError(f"No server provides tool {name!r}", "validation")

        # A stale route without a connection is known to fail before either a
        # provider hook or the session can dispatch a request.
        conn = self._servers.get(server_name)
        if conn is None:
            raise MCPToolCallError(
                f"No active connection for MCP server {server_name!r}",
                "transport_before_send",
            )
        context = trace_context or TraceContext(
            ticket_id=conn.ticket_id,
            agent_id=conn.agent_id,
        )
        correlation_id = context.mcp_correlation_request_id or uuid.uuid4().hex
        request_hash = (
            context.idempotency_request_hash
            or hashlib.sha256(
                json.dumps(
                    {"tool": name, "arguments": arguments},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
        )
        operation_key = context.idempotency_key or (
            f"mcp-delivery:{context.ticket_id or 'external'}:{conn.name}:{context.action_id}"
        )
        context = TraceContext.model_validate(
            context.model_dump()
            | {
                "mcp_server": conn.name,
                "mcp_session_id": conn.session_id,
                "mcp_correlation_request_id": correlation_id,
                "idempotency_key": operation_key,
                "idempotency_request_hash": request_hash,
            }
        )

        def redact_client_message(message: str) -> str:
            return get_shared_redactor().redact_string(
                context.ticket_id or conn.ticket_id or "unknown", message
            )[:4096]

        metadata = {
            "traceparent": f"00-{context.trace_id}-{context.action_id}-01",
            "agentic-perf": {
                "ticket_id": context.ticket_id,
                "agent_id": context.agent_id,
                "invocation_id": str(context.invocation_id or ""),
                "trace_id": context.trace_id,
                "action_id": context.action_id,
                "parent_action_id": context.parent_action_id,
                "iteration": context.iteration,
                "tool_call_id": context.tool_call_id,
                "mcp_server": conn.name,
                "mcp_session_id": conn.session_id,
                "correlation_request_id": correlation_id,
                "idempotency_key": context.idempotency_key,
                "idempotency_request_hash": context.idempotency_request_hash,
            },
        }

        # Pre-call hook: provider-specific guards
        # (e.g., Jumpstarter one-connect, timeout).
        if self.pre_call_hook is not None:
            try:
                short_circuit = await self.pre_call_hook(name, arguments)
            except MCPToolCallError:
                raise
            except Exception as e:
                # Hooks can dispatch themselves (for example, Jumpstarter's
                # connection guard), so this boundary cannot prove no request
                # was sent.  Preserve explicitly classified failures only.
                raise MCPToolCallError(str(e), "ambiguous_after_send") from e
            if short_circuit is not None:
                return short_circuit

        try:
            self._record_boundary(
                conn, LifecycleState.REQUEST_SENT, context=context, tool_name=name
            )
            if conn.session is None:
                raise RuntimeError("MCP session closed before tool dispatch")
            result = await conn.session.call_tool(name, arguments, meta=metadata)
        except asyncio.CancelledError:
            self._record_boundary(
                conn,
                LifecycleState.CANCELLED,
                context=context,
                tool_name=name,
                outcome=OperationOutcome.CANCELLED,
            )
            raise
        except Exception as e:
            self._record_boundary(
                conn,
                LifecycleState.FAILED,
                context=context,
                tool_name=name,
                outcome=OperationOutcome.FAILURE,
            )
            raise MCPToolCallError(
                redact_client_message(str(e)), "ambiguous_after_send"
            ) from e
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        content = "\n".join(parts) if parts else ""
        if result.isError:
            self._record_boundary(
                conn,
                LifecycleState.FAILED,
                context=context,
                tool_name=name,
                outcome=OperationOutcome.FAILURE,
            )
            raise MCPToolCallError(
                redact_client_message(content), "intentional_agent_retry"
            )
        self._record_boundary(
            conn,
            LifecycleState.RESPONSE_RECEIVED,
            context=context,
            tool_name=name,
            outcome=OperationOutcome.SUCCESS,
        )

        # Post-call hook: provider-specific response
        # trimming (e.g., Jumpstarter verbose output).
        if self.post_call_hook is not None:
            content = self.post_call_hook(name, content)

        return content

    def _record_boundary(
        self,
        conn: _ServerConnection,
        state: LifecycleState,
        *,
        context: TraceContext | None = None,
        tool_name: str | None = None,
        outcome: OperationOutcome | None = None,
    ) -> None:
        context = (
            context
            or current_trace_context()
            or TraceContext(
                ticket_id=conn.ticket_id,
                agent_id=conn.agent_id,
                mcp_correlation_request_id=uuid.uuid4().hex,
            )
        )
        if not context.ticket_id:
            return
        terminal = state in {
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.RESPONSE_RECEIVED,
        }
        event = TraceEventV1(
            ticket_id=context.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            tool_call_id=context.tool_call_id,
            producer=ProducerIdentity(component="mcp_client", pid=os.getpid()),
            mcp=MCPIdentity(
                server=conn.name,
                transport=conn.transport,
                session_id=conn.session_id,
                correlation_request_id=context.mcp_correlation_request_id,
                server_pid=conn.subprocess_pid,
            ),
            action=ActionDescriptor(type=ActionType.MCP, phase=tool_name),
            lifecycle=LifecycleDescriptor(state=state),
            duration_ms=0 if terminal else None,
            outcome=outcome if terminal else None,
            attributes={
                "endpoint": conn.endpoint,
                "reconnect_generation": conn.reconnect_generation,
                "client_process_identity": conn.client_process_identity,
                "subprocess_pid_capture": conn.subprocess_pid_capture,
            },
        )
        self.audit_events.append(event)
        if self._audit_hook is not None:
            self._audit_hook(event)
        if self._trace_client is not None:
            self._trace_client.record(event)

    async def disconnect(self) -> None:
        for conn in list(self._servers.values()):
            self._record_boundary(conn, LifecycleState.DISCONNECTED)
            conn._shutdown.set()
            if conn._task is not None and not conn._task.done():
                try:
                    await asyncio.wait_for(conn._task, timeout=3)
                except TimeoutError:
                    conn._task.cancel()
                    await conn._task
                except (Exception, BaseException):
                    pass
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
        ap_home = Path(
            os.environ.get(
                "AGENTIC_PERF_HOME",
                str(Path.home() / ".agentic-perf"),
            )
        )
        secrets_dir = str(ap_home / "secrets")

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
