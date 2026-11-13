"""Controller-scoped MCP adapter for Crucible contract v3.

The adapter owns a private ``AgentMCPClient`` connection so Crucible tools are
not registered as model-facing tools. Callers continue to use the existing
benchmark and review tools, which enforce validation, approval, ownership, and
durable-operation semantics before dispatching to this adapter.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agents.mcp_client import AgentMCPClient
from providers.tracing import TraceContext, current_trace_context


class CrucibleMCPError(RuntimeError):
    """A deterministic Crucible MCP setup or contract failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CrucibleMCPSettings:
    """Validated settings for the controller MCP endpoint."""

    port: int = 8889
    path: str = "/mcp"
    secret: str = "crucible-mcp/token"
    trust: bool = False
    timeout_seconds: float = 30.0
    result_readiness_timeout_seconds: float = 300.0
    controller_overrides: dict[str, dict[str, Any]] | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "CrucibleMCPSettings":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise CrucibleMCPError(
                "invalid_configuration",
                "crucible_mcp must be a JSON object",
            )
        raw_port = raw.get("port", 8889)
        raw_timeout = raw.get("timeout_seconds", 30.0)
        raw_readiness_timeout = raw.get("result_readiness_timeout_seconds", 300.0)
        if (
            isinstance(raw_port, bool)
            or not isinstance(raw_port, int)
            or isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or isinstance(raw_readiness_timeout, bool)
            or not isinstance(raw_readiness_timeout, (int, float))
        ):
            raise CrucibleMCPError(
                "invalid_configuration",
                "crucible_mcp numeric settings are invalid",
            )
        port = int(raw_port)
        timeout_seconds = float(raw_timeout)
        readiness_timeout = float(raw_readiness_timeout)
        path = raw.get("path", "/mcp")
        secret = raw.get("secret", "crucible-mcp/token")
        trust = raw.get("trust", False)
        overrides = raw.get("controller_overrides", {})
        if (
            not 1 <= port <= 65535
            or not isinstance(path, str)
            or not path.startswith("/")
            or not isinstance(secret, str)
            or not secret.strip()
            or not isinstance(trust, bool)
            or timeout_seconds <= 0
            or readiness_timeout <= 0
            or not math.isfinite(timeout_seconds)
            or not math.isfinite(readiness_timeout)
            or not isinstance(overrides, dict)
        ):
            raise CrucibleMCPError(
                "invalid_configuration",
                "crucible_mcp settings are invalid",
            )
        validated_overrides: dict[str, dict[str, Any]] = {}
        allowed_override_keys = {"url", "secret", "trust"}
        for controller, override in overrides.items():
            if not isinstance(controller, str) or not controller.strip():
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "crucible_mcp controller override keys are invalid",
                )
            if not isinstance(override, dict) or set(override) - allowed_override_keys:
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "crucible_mcp controller overrides are invalid",
                )
            override_url = override.get("url")
            if override_url is not None and not _valid_http_url(override_url):
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "crucible_mcp controller override URL is invalid",
                )
            override_secret = override.get("secret")
            if override_secret is not None and (
                not isinstance(override_secret, str) or not override_secret.strip()
            ):
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "crucible_mcp controller override secret is invalid",
                )
            override_trust = override.get("trust")
            if override_trust is not None and not isinstance(override_trust, bool):
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "crucible_mcp controller override trust is invalid",
                )
            validated_overrides[controller] = dict(override)
        return cls(
            port=port,
            path=path,
            secret=secret,
            trust=trust,
            timeout_seconds=timeout_seconds,
            result_readiness_timeout_seconds=readiness_timeout,
            controller_overrides=validated_overrides,
        )


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and bool(parsed.hostname)
        and (port is None or 1 <= port <= 65535)
        and not parsed.query
        and not parsed.fragment
    )


class CrucibleMCPAdapter:
    """One ticket/controller-bound connection to a Crucible MCP server."""

    SERVER_NAME = "crucible-mcp"
    RESOURCE_URI_PREFIXES = ("crucible://", "context://crucible/")

    def __init__(
        self,
        *,
        controller: str,
        ticket_id: str,
        agent_id: str = "",
        settings: CrucibleMCPSettings | None = None,
        secrets_provider: Any = None,
        client: AgentMCPClient | None = None,
    ) -> None:
        if not isinstance(controller, str) or not controller.strip():
            raise CrucibleMCPError(
                "invalid_controller",
                "A Crucible controller is required",
            )
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise CrucibleMCPError(
                "invalid_ticket",
                "A ticket ID is required for the Crucible MCP connection",
            )
        self.controller = controller.strip()
        self.ticket_id = ticket_id.strip()
        self.agent_id = agent_id
        self._settings = settings or CrucibleMCPSettings()
        self._secrets_provider = secrets_provider
        self._client = client
        self._connected = False
        self.contract_info: dict[str, Any] = {}

    @classmethod
    def from_config(
        cls,
        *,
        controller: str,
        ticket_id: str,
        agent_id: str = "",
        secrets_provider: Any = None,
        client: AgentMCPClient | None = None,
    ) -> "CrucibleMCPAdapter":
        """Build an adapter from the instance's ``crucible_mcp`` settings."""
        from paths import CONFIG_PATH

        raw_config: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "The agentic-perf configuration is invalid",
                ) from exc
            if not isinstance(loaded, dict):
                raise CrucibleMCPError(
                    "invalid_configuration",
                    "The agentic-perf configuration is invalid",
                )
            raw_config = loaded
        return cls(
            controller=controller,
            ticket_id=ticket_id,
            agent_id=agent_id,
            settings=CrucibleMCPSettings.from_raw(raw_config.get("crucible_mcp")),
            secrets_provider=secrets_provider,
            client=client,
        )

    @property
    def server_name(self) -> str:
        return self.SERVER_NAME

    @property
    def settings(self) -> CrucibleMCPSettings:
        return self._settings

    @property
    def connected(self) -> bool:
        return self._connected

    def _trace_context(self) -> TraceContext:
        return current_trace_context() or TraceContext(
            ticket_id=self.ticket_id, agent_id=self.agent_id
        )

    def _controller_override(self) -> dict[str, Any]:
        overrides = self._settings.controller_overrides or {}
        override = overrides.get(self.controller, {})
        return override if isinstance(override, dict) else {}

    def endpoint(self) -> str:
        override = self._controller_override()
        configured_url = override.get("url")
        if isinstance(configured_url, str) and configured_url.strip():
            return configured_url.strip()
        controller = self.controller
        if ":" in controller and not controller.startswith("["):
            controller = f"[{controller}]"
        return f"http://{controller}:{self._settings.port}{self._settings.path}"

    def _secret_name(self) -> str:
        override = self._controller_override()
        secret = override.get("secret", self._settings.secret)
        if not isinstance(secret, str) or not secret.strip():
            raise CrucibleMCPError(
                "invalid_configuration",
                "The Crucible MCP secret name is invalid",
            )
        return secret.strip()

    def _trust(self) -> bool:
        override = self._controller_override()
        trust = override.get("trust", self._settings.trust)
        if not isinstance(trust, bool):
            raise CrucibleMCPError(
                "invalid_configuration",
                "The Crucible MCP trust setting is invalid",
            )
        return trust

    async def _resolve_token(self) -> str:
        if self._secrets_provider is None:
            from agents.server_utils import build_secrets_provider

            self._secrets_provider = build_secrets_provider()
        token = await self._secrets_provider.get_secret(self._secret_name())
        if not isinstance(token, str) or not token.strip():
            raise CrucibleMCPError(
                "secret_missing",
                "The Crucible MCP bearer token is missing",
            )
        return token.strip()

    async def connect(self) -> None:
        """Connect and require Crucible MCP contract v3 with execution."""
        if self._connected:
            return
        token = await self._resolve_token()
        if self._client is None:
            self._client = AgentMCPClient(trace_context=self._trace_context())
        await self._client.connect_streamable_http(
            url=self.endpoint(),
            name=self.SERVER_NAME,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._settings.timeout_seconds,
            trust=self._trust(),
        )
        try:
            self._client.register_resource_scope(
                self.SERVER_NAME,
                self.RESOURCE_URI_PREFIXES,
            )
            raw_info = await self._client.call_tool("crucible_info", {})
            info = json.loads(raw_info)
        except (TypeError, json.JSONDecodeError) as exc:
            await self._client.disconnect()
            raise CrucibleMCPError(
                "invalid_contract_response",
                "Crucible MCP contract information is not valid JSON",
            ) from exc
        except Exception:
            await self._client.disconnect()
            raise
        try:
            if not isinstance(info, dict):
                raise CrucibleMCPError(
                    "invalid_contract_response",
                    "Crucible MCP contract information is invalid",
                )
            contract_version = str(info.get("mcp_contract_version", ""))
            execution_supported = info.get("execution_supported")
            if contract_version != "3" or execution_supported is not True:
                raise CrucibleMCPError(
                    "unsupported_controller",
                    "The Crucible controller does not support MCP contract v3 execution",
                )
        except Exception:
            await self._client.disconnect()
            raise
        self.contract_info = info
        self._connected = True

    def _require_client(self) -> AgentMCPClient:
        if self._client is None or not self._connected:
            raise CrucibleMCPError(
                "not_connected",
                "The Crucible MCP adapter is not connected",
            )
        return self._client

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call one Crucible MCP tool through the private client."""
        if not self._connected:
            await self.connect()
        client = self._require_client()
        return await client.call_tool(
            name,
            arguments,
            trace_context=self._trace_context(),
        )

    async def list_userenvs(self) -> dict[str, Any]:
        """Return Crucible userenvs from the controller MCP server."""
        raw = await self.call_tool("list_userenvs", {})
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CrucibleMCPError(
                "invalid_userenv_response",
                "Crucible MCP userenv response is not valid JSON",
            ) from exc
        if not isinstance(result, dict) or not all(
            isinstance(result.get(key), list)
            for key in ("official", "external", "deprecated")
        ):
            raise CrucibleMCPError(
                "invalid_userenv_response",
                "Crucible MCP userenv response is invalid",
            )
        return result

    async def list_resources(self) -> list[dict[str, Any]]:
        """List Crucible MCP resources through the audited client API."""
        if not self._connected:
            await self.connect()
        client = self._require_client()
        return await client.list_resources(self.SERVER_NAME)

    async def read_resource(self, uri: str) -> str:
        """Read one scoped Crucible MCP resource."""
        if not self._connected:
            await self.connect()
        client = self._require_client()
        return await client.read_resource(self.SERVER_NAME, uri)

    async def close(self) -> None:
        """Close the private MCP connection."""
        if self._client is not None:
            await self._client.disconnect()
        self._connected = False
