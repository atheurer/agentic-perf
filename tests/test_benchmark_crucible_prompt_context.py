"""Regression checks for MCP-first context guidance in the Crucible prompt.

Issue #1443: the benchmark prompt must not require installed subproject file
reads that the controller MCP cannot serve. Context read failures have to be
non-blocking, and the prompt must name capabilities that exist in contract v3.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.base import AgentBase
from agents.benchmark.agent import BenchmarkAgent

PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent / "agents" / "benchmark" / "prompts"
)


def _crucible_fragment() -> str:
    return AgentBase._load_prompt_fragment(PROMPTS_DIR, "crucible")


def test_fragment_uses_served_context_refs() -> None:
    prompt = _crucible_fragment()

    assert "docs/run-files" in prompt
    assert "docs/endpoints" in prompt
    assert "docs/tool-collection" in prompt
    assert "benchmark/<name>" in prompt
    assert "parameter_validation" in prompt
    assert "runfile_contract" in prompt


def test_fragment_drops_unservable_subproject_file_reads() -> None:
    prompt = _crucible_fragment()

    assert "subprojects/benchmarks" not in prompt
    assert "software-document ref" not in prompt
    assert "from_ref" not in prompt
    assert "Do not read a subproject's" in prompt


def test_fragment_treats_context_read_failure_as_non_blocking() -> None:
    prompt = _crucible_fragment()

    assert "non-blocking" in prompt
    assert "Record which ref or document" in prompt
    assert "Call `request_clarification` only when" in prompt


def test_fragment_downgrades_presets_and_roles_to_optional() -> None:
    prompt = _crucible_fragment()

    assert "Named presets are optional" in prompt
    assert "`role` is optional" in prompt


def test_crucible_system_prompt_includes_mcp_context_guidance() -> None:
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._skill_provider = SimpleNamespace(default_harness="crucible")
    agent.tools = [SimpleNamespace(name="get_skill_context", owner_harness=None)]
    ticket = {"custom_fields": {"directives": {"harness": "crucible"}}}

    prompt = agent._system_prompt(ticket)

    assert "docs/run-files" in prompt
    assert "benchmark/<name>" in prompt
