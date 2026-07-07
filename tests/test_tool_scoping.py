"""Tests for benchmark agent tool scoping (#201)."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.benchmark.agent import BenchmarkAgent


class TestHarnessToolScoping:
    def _make_agent(self):
        return BenchmarkAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:8090",
        )

    def _make_tools(self, names: list[str]):
        tools = []
        for name in names:
            t = MagicMock()
            t.name = name
            tools.append(t)
        return tools

    def test_boot_time_filters_to_allowed(self):
        agent = self._make_agent()
        agent.tools = self._make_tools(
            [
                "read_skill",
                "set_ssh_context",
                "check_host",
                "execute_boot_time_test",
                "submit_benchmark_result",
                "request_clarification",
                "execute_command",
                "execute_benchmark",
                "get_execution_config",
                "get_benchmark_params",
                "write_remote_file",
                "read_remote_file",
            ]
        )
        ticket = {
            "custom_fields": {
                "directives": {"harness": "boot-time"},
            },
        }
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert names == {
            "read_skill",
            "set_ssh_context",
            "check_host",
            "execute_boot_time_test",
            "submit_benchmark_result",
            "request_clarification",
        }

    def test_unlisted_harness_keeps_all_tools(self):
        agent = self._make_agent()
        all_names = [
            "read_skill",
            "execute_benchmark",
            "execute_command",
            "get_execution_config",
        ]
        agent.tools = self._make_tools(all_names)
        ticket = {
            "custom_fields": {
                "directives": {"harness": "crucible"},
            },
        }
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert names == set(all_names)

    def test_no_harness_directive_keeps_all_tools(self):
        agent = self._make_agent()
        all_names = [
            "read_skill",
            "execute_benchmark",
            "execute_command",
        ]
        agent.tools = self._make_tools(all_names)
        ticket = {"custom_fields": {}}
        agent._apply_tool_scoping(ticket)
        names = {t.name for t in agent.tools}
        assert names == set(all_names)

    def test_empty_directives_keeps_all_tools(self):
        agent = self._make_agent()
        all_names = ["read_skill", "execute_command"]
        agent.tools = self._make_tools(all_names)
        ticket = {
            "custom_fields": {"directives": {}},
        }
        agent._apply_tool_scoping(ticket)
        assert len(agent.tools) == 2
