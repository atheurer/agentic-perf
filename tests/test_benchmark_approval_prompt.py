"""Tests for code-driven approval step in benchmark agent prompt (#1294)."""

from __future__ import annotations

from types import SimpleNamespace

from agents.benchmark.agent import BenchmarkAgent
from providers.llm.base import ToolDefinition


class TestApprovalStepSelection:
    """Verify _system_prompt selects the correct approval step."""

    def _make_agent(self, tool_names, owner_harnesses=None):
        """Create a minimal BenchmarkAgent with specified tools."""
        owners = owner_harnesses or {}
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._skill_provider = SimpleNamespace(default_harness="crucible")
        agent.tools = [
            ToolDefinition(
                name=t,
                description="",
                input_schema={},
                owner_harness=owners.get(t),
            )
            for t in tool_names
        ]
        return agent

    def _ticket(self, directives=None, harness="test"):
        directives = directives or {}
        if "harness" not in directives:
            directives["harness"] = harness
        return {
            "custom_fields": {
                "directives": directives,
                "harness_name": harness,
            },
        }

    def test_crucible_gets_validated_approval(self):
        agent = self._make_agent(
            ["present_runfile_for_approval", "validate_benchmark"],
            owner_harnesses={
                "present_runfile_for_approval": "crucible",
                "validate_benchmark": "crucible",
            },
        )
        ticket = self._ticket(harness="crucible")
        # Scope tools first (production order)
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "present_runfile_for_approval" in prompt
        assert "Proceed to execution" not in prompt
        assert "Run-file approval replies" in prompt

    def test_boot_time_skips_approval(self):
        agent = self._make_agent(
            ["execute_boot_time_test"],
            owner_harnesses={"execute_boot_time_test": "boot-time"},
        )
        ticket = self._ticket(harness="boot-time")
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "Proceed to execution" in prompt
        assert "present_runfile_for_approval" not in prompt
        assert "Run-file approval replies" not in prompt

    def test_explicit_true_forces_approval(self):
        agent = self._make_agent(
            ["execute_boot_time_test"],
            owner_harnesses={"execute_boot_time_test": "boot-time"},
        )
        ticket = self._ticket(
            directives={
                "harness": "boot-time",
                "user_pre_run_approval": True,
            },
            harness="boot-time",
        )
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "request_clarification" in prompt
        assert "explicitly requested" in prompt
        assert "Run-file approval replies" not in prompt

    def test_explicit_false_skips_with_no_tools(self):
        agent = self._make_agent(
            ["execute_boot_time_test"],
            owner_harnesses={"execute_boot_time_test": "boot-time"},
        )
        ticket = self._ticket(
            directives={
                "harness": "boot-time",
                "user_pre_run_approval": False,
            },
            harness="boot-time",
        )
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "Proceed to execution" in prompt

    def test_validated_step_includes_approval_replies(self):
        agent = self._make_agent(
            ["present_runfile_for_approval", "validate_benchmark"],
            owner_harnesses={
                "present_runfile_for_approval": "crucible",
                "validate_benchmark": "crucible",
            },
        )
        ticket = self._ticket(harness="crucible")
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "Run-file approval replies" in prompt
        assert "Interpret it semantically" in prompt

    def test_explicit_false_auto_approves_with_tools(self):
        agent = self._make_agent(
            ["present_runfile_for_approval", "validate_benchmark"],
            owner_harnesses={
                "present_runfile_for_approval": "crucible",
                "validate_benchmark": "crucible",
            },
        )
        ticket = self._ticket(
            directives={
                "harness": "crucible",
                "user_pre_run_approval": False,
            },
            harness="crucible",
        )
        agent._apply_tool_scoping(ticket)
        prompt = agent._system_prompt(ticket)
        assert "Automatic approval" in prompt
        assert "opted out" in prompt
        assert "present_runfile_for_approval" not in prompt
        assert "Run-file approval replies" not in prompt
