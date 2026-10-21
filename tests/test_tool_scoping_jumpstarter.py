"""Tests for Jumpstarter-specific tool scoping."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from providers.llm.base import ToolDefinition


class TestProvisioningToolScoping:
    """Test _apply_tool_scoping on the provisioning agent."""

    def _make_agent(self, skill_provider=None):
        from agents.provisioning.agent import ProvisioningAgent

        agent = ProvisioningAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
            skill_provider=skill_provider,
        )
        # Simulate tools that would be loaded from MCP
        agent.tools = [
            ToolDefinition(name=n, description="", input_schema={})
            for n in [
                "jmp_connect",
                "jmp_run",
                "deploy_secret",
                "install_harness",
                "get_private_config",
                "install_packages",
                "set_ssh_context",
                "check_host",
                "check_host_prerequisites",
                "submit_provisioning_result",
                "request_clarification",
            ]
        ]
        return agent

    def test_boot_time_scoping(self):
        agent = self._make_agent()
        agent._harness_self_installing = True
        ticket = {"custom_fields": {"directives": {"harness": "boot-time"}}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert "jmp_connect" in names
        assert "jmp_run" in names
        assert "set_ssh_context" in names
        assert "submit_provisioning_result" in names
        # Denied tools should be hidden
        assert "deploy_secret" not in names
        assert "install_harness" not in names
        assert "get_private_config" not in names
        assert "install_packages" not in names

    def test_arcaflow_scoping(self):
        agent = self._make_agent()
        agent._harness_self_installing = True
        ticket = {"custom_fields": {"directives": {"harness": "arcaflow-plugins"}}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert "jmp_run" in names
        assert "check_host_prerequisites" in names
        assert "deploy_secret" not in names
        assert "install_harness" not in names

    def test_self_installing_prompt_uses_read_only_prerequisite_check(self):
        agent = self._make_agent()
        agent._harness_self_installing = True
        prompt = agent._system_prompt(
            {"custom_fields": {"directives": {"harness": "arcaflow-plugins"}}}
        )
        assert "read-only check_host_prerequisites" in prompt
        assert "do not call get_private_config" in prompt
        assert "Podman" in prompt
        assert "benchmark agent owns workload execution" in prompt

    def test_crucible_no_scoping(self):
        """Non-self-installing harnesses keep all tools."""
        agent = self._make_agent()
        ticket = {"custom_fields": {"directives": {"harness": "crucible"}}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert "deploy_secret" in names
        assert "install_harness" in names
        assert "jmp_run" in names

    def test_no_harness_no_scoping(self):
        """Missing harness directive keeps all tools."""
        agent = self._make_agent()
        ticket = {"custom_fields": {"directives": {}}}
        agent._apply_tool_scoping(ticket)
        assert len(agent.tools) == 11

    def test_crucible_harness_keeps_install_tools(self):
        """Non-self-installing harnesses keep install tools after scoping."""
        agent = self._make_agent()
        ticket = {"custom_fields": {"directives": {"harness": "crucible"}}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert "install_harness" in names
        assert "deploy_secret" in names

    def test_default_crucible_keeps_install_tools_when_directive_is_omitted(self):
        agent = self._make_agent(
            skill_provider=SimpleNamespace(default_harness="crucible")
        )
        ticket = {"custom_fields": {"directives": {}}}

        agent._apply_tool_scoping(ticket)

        names = {tool.name for tool in agent.tools}
        assert "install_harness" in names
        assert "deploy_secret" in names

    @pytest.mark.asyncio
    async def test_default_crucible_jumpstarter_does_not_auto_complete(self):
        agent = self._make_agent(
            skill_provider=SimpleNamespace(default_harness="crucible")
        )
        ticket = {
            "id": "PERF-DEFAULT-CRUCIBLE",
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "directives": {},
                "platform_ready": True,
                "hosts_provisioned": ["10.0.0.8"],
            },
        }
        mcp = AsyncMock()
        mcp.list_tools = AsyncMock(
            return_value=[
                ToolDefinition(name="install_harness", description="", input_schema={})
            ]
        )

        with (
            patch("agents.provisioning.agent.AgentMCPClient", return_value=mcp),
            patch.object(
                agent, "_get_ticket", new_callable=AsyncMock, return_value=ticket
            ),
            patch.object(
                agent, "_auto_complete_jumpstarter", new_callable=AsyncMock
            ) as auto_complete,
            patch(
                "agents.provisioning.agent.AgentBase.run", new_callable=AsyncMock
            ) as base_run,
        ):
            await agent.run(ticket["id"])

        auto_complete.assert_not_awaited()
        base_run.assert_awaited_once_with(ticket["id"])
        assert any(tool.name == "install_harness" for tool in agent.tools)
        assert (
            'get_skill_context(subject="harness/crucible", operation="bootstrap")'
            in agent._system_prompt(ticket)
        )


class TestBenchmarkToolScoping:
    """Test ownership metadata used to scope benchmark agent tools."""

    @pytest.mark.asyncio
    async def test_arcaflow_scoping(self):
        from agents.benchmark import arcaflow_plugin_server
        from agents.benchmark.agent import _WORKFLOW_TOOL
        from agents.tool_ownership import owner_harness_from_tool
        from providers.skills.arcaflow_mcp import (
            PLUGIN_DESCRIBE,
            PLUGIN_LIST,
            WORKFLOW_EXECUTE,
            WORKFLOW_EXECUTION_CANCEL,
            WORKFLOW_EXECUTION_OUTPUT,
            WORKFLOW_EXECUTION_STATUS,
            WORKFLOW_INPUT_BUILD,
            WORKFLOW_INPUT_EXPORT,
            WORKFLOW_INPUT_VALIDATE,
            WORKFLOW_LIST,
            WORKFLOW_LOAD,
            register_arcaflow_tool_ownership,
        )

        plugin_tools = {
            name: await arcaflow_plugin_server.mcp.get_tool(name)
            for name in ("get_plugin_schema", "execute_arcaflow_plugin")
        }
        assert all(
            owner_harness_from_tool(tool) == "arcaflow-plugins"
            for tool in plugin_tools.values()
        )
        assert _WORKFLOW_TOOL.owner_harness == "arcaflow-workflows"

        class OwnershipRegistry:
            def __init__(self):
                self.owners = {}

            def require_tool_ownership(self, server_name):
                assert server_name == "arcaflow"

            def register_tool_owner(
                self,
                server_name,
                tool_name,
                owner_harness,
                *,
                visible_to_model=True,
            ):
                assert server_name == "arcaflow"
                self.owners[tool_name] = (owner_harness, visible_to_model)

        registry = OwnershipRegistry()
        register_arcaflow_tool_ownership(registry)

        assert {
            name
            for name, (owner, visible) in registry.owners.items()
            if owner == "arcaflow-plugins" and visible
        } == {PLUGIN_LIST, PLUGIN_DESCRIBE}
        assert {
            name
            for name, (owner, visible) in registry.owners.items()
            if owner == "arcaflow-workflows" and visible
        } == {
            WORKFLOW_LOAD,
            WORKFLOW_LIST,
            WORKFLOW_INPUT_BUILD,
            WORKFLOW_INPUT_VALIDATE,
        }
        assert {
            name
            for name, (owner, visible) in registry.owners.items()
            if owner == "arcaflow-workflows" and not visible
        } == {
            WORKFLOW_INPUT_EXPORT,
            WORKFLOW_EXECUTE,
            WORKFLOW_EXECUTION_STATUS,
            WORKFLOW_EXECUTION_CANCEL,
            WORKFLOW_EXECUTION_OUTPUT,
        }

    def test_external_tool_filter_keeps_local_and_enabled_workflow_tools(self):
        from agents.benchmark.agent import _filter_external_tools

        tools = [
            ToolDefinition(name="execute_benchmark", description="", input_schema={}),
            ToolDefinition(name="workflow_load", description="", input_schema={}),
            ToolDefinition(name="workflow_execute", description="", input_schema={}),
        ]
        filtered = _filter_external_tools(
            tools,
            {
                "execute_benchmark": "benchmark",
                "workflow_load": "arcaflow",
                "workflow_execute": "arcaflow",
            },
            ["arcaflow"],
            {"workflow_load"},
        )
        assert {tool.name for tool in filtered} == {
            "execute_benchmark",
            "workflow_load",
        }

    @pytest.mark.asyncio
    async def test_boot_time_scoping(self):
        from agents.benchmark import server
        from agents.tool_ownership import owner_harness_from_tool

        tool = await server.mcp.get_tool("execute_boot_time_test")
        assert owner_harness_from_tool(tool) == "boot-time"

    @pytest.mark.asyncio
    async def test_crucible_scoping_excludes_legacy_docs_but_keeps_validation(self):
        from agents.benchmark import server
        from agents.benchmark.agent import BenchmarkAgent
        from agents.tool_ownership import owner_harness_from_tool

        validation_tool = await server.mcp.get_tool("validate_benchmark")
        assert owner_harness_from_tool(validation_tool) == "crucible"

        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent.tools = [
            ToolDefinition(
                name=name,
                description="",
                input_schema={},
                owner_harness=owner,
            )
            for name, owner in (
                ("validate_benchmark", owner_harness_from_tool(validation_tool)),
                ("execute_benchmark", None),
                ("read_skills", None),
                ("list_harness_docs", None),
                ("read_harness_doc", None),
                ("get_benchmark_params", None),
            )
        ]
        agent._apply_tool_scoping(
            {"custom_fields": {"directives": {"harness": "crucible"}}}
        )
        assert {tool.name for tool in agent.tools} == {
            "validate_benchmark",
            "execute_benchmark",
        }


@pytest.mark.asyncio
async def test_arcaflow_plugin_server_failure_is_required_only_for_plugin_harness():
    from agents.benchmark.agent import _connect_arcaflow_plugin_server

    optional_mcp = AsyncMock()
    optional_mcp.connect_ticket_server.side_effect = RuntimeError("startup failed")
    await _connect_arcaflow_plugin_server(
        optional_mcp,
        "arcaflow_plugin_server.py",
        ticket_id="PERF-OPTIONAL",
        state_store_url="http://state-store.test",
        agent_name="benchmark-agent",
        required=False,
    )
    optional_mcp.disconnect.assert_not_awaited()

    required_mcp = AsyncMock()
    required_mcp.connect_ticket_server.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        await _connect_arcaflow_plugin_server(
            required_mcp,
            "arcaflow_plugin_server.py",
            ticket_id="PERF-PLUGINS",
            state_store_url="http://state-store.test",
            agent_name="benchmark-agent",
            required=True,
        )
    required_mcp.disconnect.assert_awaited_once()
