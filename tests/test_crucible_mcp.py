from __future__ import annotations

import json
from typing import Any

import pytest

import agents.benchmark.server as benchmark
import agents.infra.server as infra
import agents.review.server as review
from agents.crucible_mcp import (
    CrucibleMCPAdapter,
    CrucibleMCPError,
    CrucibleMCPSettings,
)
from agents.mcp_client import AgentMCPClient, MCPToolCallError, _ServerConnection


class SecretProvider:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def get_secret(self, name: str) -> str:
        self.names.append(name)
        return "secret-token"


class FakeMCPClient:
    def __init__(self, info: str) -> None:
        self.info = info
        self.connected = False
        self.disconnected = False
        self.resource_scopes: list[tuple[str, tuple[str, ...]]] = []
        self.connect_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect_streamable_http(self, **kwargs: Any) -> None:
        self.connect_calls.append(kwargs)
        self.connected = True

    def register_resource_scope(
        self, server_name: str, prefixes: tuple[str, ...]
    ) -> None:
        self.resource_scopes.append((server_name, prefixes))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return self.info

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False


def adapter(
    info: str = json.dumps({"mcp_contract_version": "3", "execution_supported": True}),
    settings: CrucibleMCPSettings | None = None,
) -> tuple[CrucibleMCPAdapter, FakeMCPClient, SecretProvider]:
    client = FakeMCPClient(info)
    secrets = SecretProvider()
    instance = CrucibleMCPAdapter(
        controller="controller.example",
        ticket_id="PERF-MCP",
        agent_id="benchmark-agent",
        settings=settings or CrucibleMCPSettings(),
        secrets_provider=secrets,
        client=client,
    )
    return instance, client, secrets


@pytest.mark.asyncio
async def test_adapter_connects_with_secret_and_contract_v3():
    instance, client, secrets = adapter()
    await instance.connect()

    assert secrets.names == ["crucible-mcp/token"]
    assert client.connect_calls[0]["url"] == "http://controller.example:8889/mcp"
    assert client.connect_calls[0]["headers"] == {
        "Authorization": "Bearer secret-token"
    }
    assert client.resource_scopes == [
        ("crucible-mcp", CrucibleMCPAdapter.RESOURCE_URI_PREFIXES)
    ]
    assert client.calls == [("crucible_info", {})]
    assert instance.connected is True


@pytest.mark.asyncio
async def test_adapter_rejects_non_v3_contract_and_closes_connection():
    instance, client, _ = adapter(
        json.dumps({"mcp_contract_version": "2", "execution_supported": True})
    )
    with pytest.raises(Exception, match="MCP contract v3"):
        await instance.connect()
    assert client.disconnected is True
    assert instance.connected is False


@pytest.mark.asyncio
async def test_adapter_rejects_execution_unsupported_contract():
    instance, _, _ = adapter(
        json.dumps({"mcp_contract_version": "3", "execution_supported": False})
    )
    with pytest.raises(Exception, match="MCP contract v3"):
        await instance.connect()


@pytest.mark.asyncio
async def test_adapter_rejects_invalid_contract_json_and_closes_connection():
    instance, client, _ = adapter("{not-json")
    with pytest.raises(Exception, match="not valid JSON"):
        await instance.connect()
    assert client.disconnected is True


def test_settings_validate_endpoint_and_numeric_types():
    assert CrucibleMCPSettings.from_raw(None) == CrucibleMCPSettings()

    valid = CrucibleMCPSettings.from_raw(
        {
            "port": 9000,
            "path": "/custom",
            "secret": "override/token",
            "timeout_seconds": 12,
            "result_readiness_timeout_seconds": 45,
            "controller_overrides": {
                "controller.example": {
                    "url": "https://mcp.example/custom",
                    "secret": "controller/token",
                    "trust": True,
                }
            },
        }
    )
    instance = CrucibleMCPAdapter(
        controller="controller.example",
        ticket_id="PERF-MCP",
        settings=valid,
    )
    assert instance.endpoint() == "https://mcp.example/custom"

    ipv6 = CrucibleMCPAdapter(
        controller="2001:db8::1",
        ticket_id="PERF-MCP",
    )
    assert ipv6.endpoint() == "http://[2001:db8::1]:8889/mcp"

    invalid_settings = [
        {"port": True},
        {"port": 0},
        {"timeout_seconds": float("nan")},
        {"result_readiness_timeout_seconds": True},
        {"controller_overrides": {"controller": {"url": "file:///etc/passwd"}}},
        {"controller_overrides": {"controller": {"url": "http://u:p@example/mcp"}}},
        {"controller_overrides": {"controller": {"unexpected": True}}},
    ]
    for raw in invalid_settings:
        with pytest.raises(CrucibleMCPError) as excinfo:
            CrucibleMCPSettings.from_raw(raw)
        assert excinfo.value.code == "invalid_configuration"


@pytest.mark.asyncio
async def test_agent_mcp_resource_reads_are_server_and_uri_scoped():
    class Session:
        def __init__(self) -> None:
            self.uris: list[str] = []

        async def read_resource(self, uri: str) -> object:
            self.uris.append(uri)
            return object()

    session = Session()
    client = AgentMCPClient()
    client._servers["crucible-mcp"] = _ServerConnection(
        name="crucible-mcp",
        session=session,
        transport="streamable-http",
        connected=True,
    )
    client.register_resource_scope("crucible-mcp", ("crucible://",))

    with pytest.raises(MCPToolCallError, match="URI is not allowed"):
        await client.read_resource("crucible-mcp", "file:///etc/passwd")
    assert session.uris == []

    await client.read_resource("crucible-mcp", "crucible://AGENTS.md")
    assert session.uris == ["crucible://AGENTS.md"]


@pytest.mark.asyncio
async def test_model_servers_do_not_expose_raw_crucible_mcp_tools():
    benchmark_tools = {tool.name for tool in await benchmark.mcp.list_tools()}
    review_tools = {tool.name for tool in await review.mcp.list_tools()}
    infra_tools = {tool.name for tool in await infra.mcp.list_tools()}

    assert {"validate_benchmark", "execute_benchmark"} <= benchmark_tools
    assert "prepare_run" not in benchmark_tools
    assert "start_run" not in benchmark_tools
    assert "get_run_status" not in benchmark_tools

    assert {
        "read_run_results",
        "get_run_summary",
        "list_cdm_periods",
        "get_cdm_metric",
    } <= review_tools
    assert "get_run_artifact" not in review_tools
    assert "get_indexed_metric" not in review_tools
    assert "list_indexed_periods" not in review_tools

    assert "list_controller_userenvs" in infra_tools
    assert "run_crucible_command" not in infra_tools
    assert "list_userenvs" not in infra_tools
    tool = next(
        tool
        for tool in await infra.mcp.list_tools()
        if tool.name == "list_controller_userenvs"
    )
    assert set(tool.parameters["properties"]) == {"controller"}


def test_crucible_runtime_documentation_uses_mcp_contract_v3():
    from pathlib import Path

    repository = Path(__file__).resolve().parents[1]
    capabilities = (repository / "docs" / "capabilities.md").read_text(encoding="utf-8")
    command_broker = (repository / "docs" / "crucible-command-broker.md").read_text(
        encoding="utf-8"
    )
    workspaces = (repository / "docs" / "workspaces-and-charts.md").read_text(
        encoding="utf-8"
    )

    assert "cdm_api_requests" not in capabilities
    assert "run_crucible_command" not in capabilities
    assert "list_controller_userenvs" in capabilities
    assert "benchmark_list` | `crucible benchmark list" not in command_broker
    assert "tools_list` | `crucible tools list" not in command_broker
    assert "list_userenvs" in command_broker
    assert "does not fall back to SSH or the Crucible CLI" in command_broker
    assert "controller MCP contract v3" in workspaces
