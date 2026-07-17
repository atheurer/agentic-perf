"""Tests for Jumpstarter-specific tool scoping."""

from __future__ import annotations

from providers.llm.base import ToolDefinition


class TestProvisioningToolScoping:
    """Test _apply_tool_scoping on the provisioning agent."""

    def _make_agent(self):
        from unittest.mock import AsyncMock

        from agents.provisioning.agent import ProvisioningAgent

        agent = ProvisioningAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
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


class TestBenchmarkToolScoping:
    """Test _HARNESS_TOOLS on the benchmark agent."""

    def test_arcaflow_scoping(self):
        from agents.benchmark.agent import BenchmarkAgent

        allowed = BenchmarkAgent._HARNESS_TOOLS.get("arcaflow-plugins")
        assert allowed is not None
        assert "execute_benchmark" in allowed
        assert "get_runfile_schema" in allowed
        assert "execute_command" not in allowed

    def test_boot_time_scoping(self):
        from agents.benchmark.agent import BenchmarkAgent

        allowed = BenchmarkAgent._HARNESS_TOOLS.get("boot-time")
        assert allowed is not None
        assert "execute_boot_time_test" in allowed
        assert "execute_command" not in allowed

    def test_crucible_no_scoping(self):
        from agents.benchmark.agent import BenchmarkAgent

        assert "crucible" not in BenchmarkAgent._HARNESS_TOOLS
