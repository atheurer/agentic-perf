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
                "submit_provisioning_result",
                "request_clarification",
            ]
        ]
        return agent

    def test_boot_time_scoping(self):
        agent = self._make_agent()
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
        ticket = {"custom_fields": {"directives": {"harness": "arcaflow-plugins"}}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert "jmp_run" in names
        assert "deploy_secret" not in names
        assert "install_harness" not in names

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
        assert len(agent.tools) == 10

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
        assert "## Crucible Provisioning Notes" in agent._system_prompt(ticket)


class TestBenchmarkToolScoping:
    """Test _HARNESS_TOOLS on the benchmark agent."""

    def test_arcaflow_scoping(self):
        from agents.benchmark.agent import BenchmarkAgent

        allowed = BenchmarkAgent._HARNESS_TOOLS.get("arcaflow-plugins")
        assert allowed is not None
        assert "execute_benchmark" in allowed
        assert "get_runfile_schema" in allowed
        assert {
            "plugin_list",
            "plugin_describe",
            "workflow_load",
            "workflow_input_build",
            "workflow_input_validate",
            "workflow_input_export",
            "workflow_execute",
            "workflow_execution_status",
            "workflow_execution_cancel",
            "workflow_execution_output",
        } <= allowed
        # Unrestricted shell access is not part of any harness allowlist.
        assert "write_remote_file" not in allowed

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

    def test_boot_time_scoping(self):
        from agents.benchmark.agent import BenchmarkAgent

        allowed = BenchmarkAgent._HARNESS_TOOLS.get("boot-time")
        assert allowed is not None
        assert "execute_boot_time_test" in allowed
        # execute_benchmark is not in boot-time (uses its own tool).
        assert "execute_benchmark" not in allowed

    def test_crucible_scoping_includes_validation_before_execution(self):
        from agents.benchmark.agent import BenchmarkAgent

        allowed = BenchmarkAgent._HARNESS_TOOLS.get("crucible")
        assert allowed is not None
        assert "validate_benchmark" in allowed
        assert "execute_benchmark" in allowed
