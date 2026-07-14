"""Unit tests for the OpenAI-compatible LLM provider.

Tests message conversion, tool conversion, and response parsing using
mock data — no live API calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from providers.llm.base import ToolDefinition
from providers.llm.openai_compat import OpenAICompatLLMProvider


class TestMessageConversion:
    """Test Anthropic → OpenAI message format conversion."""

    def test_system_prompt_becomes_system_message(self):
        msgs = OpenAICompatLLMProvider._convert_messages("You are helpful.", [])
        assert msgs[0] == {"role": "system", "content": "You are helpful."}

    def test_simple_user_message(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys", [{"role": "user", "content": "hello"}]
        )
        assert msgs[1] == {"role": "user", "content": "hello"}

    def test_assistant_text_only(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys",
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll help you."},
                    ],
                },
            ],
        )
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "I'll help you."
        assert "tool_calls" not in msgs[1]

    def test_assistant_with_tool_calls(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys",
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "list_benchmarks",
                            "input": {},
                        },
                    ],
                },
            ],
        )
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Let me check."
        assert len(msgs[1]["tool_calls"]) == 1
        tc = msgs[1]["tool_calls"][0]
        assert tc["id"] == "tc_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "list_benchmarks"
        assert tc["function"]["arguments"] == "{}"

    def test_assistant_tool_call_with_complex_input(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys",
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc_2",
                            "name": "resolve_benchmark",
                            "input": {
                                "description": "network test",
                                "workload_type": "network",
                            },
                        },
                    ],
                },
            ],
        )
        tc = msgs[1]["tool_calls"][0]
        args = json.loads(tc["function"]["arguments"])
        assert args["description"] == "network test"
        assert args["workload_type"] == "network"

    def test_tool_result_becomes_tool_messages(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys",
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tc_1",
                            "content": '{"name": "uperf"}',
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tc_2",
                            "content": '{"matched": "fio"}',
                            "is_error": False,
                        },
                    ],
                },
            ],
        )
        assert len(msgs) == 3  # system + 2 tool messages
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "tc_1"
        assert msgs[1]["content"] == '{"name": "uperf"}'
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "tc_2"

    def test_tool_result_error_prefixed(self):
        msgs = OpenAICompatLLMProvider._convert_messages(
            "sys",
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tc_1",
                            "content": "Not found",
                            "is_error": True,
                        },
                    ],
                },
            ],
        )
        assert msgs[1]["content"] == "Error: Not found"

    def test_full_conversation_roundtrip(self):
        """Test a realistic multi-turn conversation."""
        msgs = OpenAICompatLLMProvider._convert_messages(
            "You are a triage agent.",
            [
                {"role": "user", "content": "Run a network test"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll look up benchmarks."},
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "list_benchmarks",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tc_1",
                            "content": '[{"name": "uperf"}]',
                            "is_error": False,
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Found uperf. Submitting."},
                    ],
                },
            ],
        )
        assert len(msgs) == 5  # system, user, assistant+tool, tool_result, assistant
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["tool_calls"][0]["function"]["name"] == "list_benchmarks"
        assert msgs[3]["role"] == "tool"
        assert msgs[4]["role"] == "assistant"
        assert msgs[4]["content"] == "Found uperf. Submitting."


class TestToolConversion:
    """Test ToolDefinition → OpenAI function format."""

    def test_basic_tool(self):
        tools = [
            ToolDefinition(
                name="check_host",
                description="Check a host",
                input_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host"],
                },
            )
        ]
        result = OpenAICompatLLMProvider._convert_tools(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        f = result[0]["function"]
        assert f["name"] == "check_host"
        assert f["description"] == "Check a host"
        assert f["parameters"]["type"] == "object"
        assert "host" in f["parameters"]["properties"]

    def test_multiple_tools(self):
        tools = [
            ToolDefinition(
                name="a", description="tool a", input_schema={"type": "object"}
            ),
            ToolDefinition(
                name="b", description="tool b", input_schema={"type": "object"}
            ),
        ]
        result = OpenAICompatLLMProvider._convert_tools(tools)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "a"
        assert result[1]["function"]["name"] == "b"


class TestResponseParsing:
    """Test OpenAI response → LLMResponse conversion."""

    def _make_response(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        finish_reason: str = "stop",
    ):
        @dataclass
        class _Function:
            name: str
            arguments: str

        @dataclass
        class _ToolCall:
            id: str
            function: _Function
            type: str = "function"

        @dataclass
        class _Message:
            content: str | None = None
            tool_calls: list | None = None
            role: str = "assistant"

        @dataclass
        class _Choice:
            message: _Message = field(default_factory=_Message)
            finish_reason: str = "stop"

        @dataclass
        class _Response:
            choices: list = field(default_factory=list)

        tc_objects = None
        if tool_calls:
            tc_objects = [
                _ToolCall(
                    id=tc["id"],
                    function=_Function(name=tc["name"], arguments=tc["arguments"]),
                )
                for tc in tool_calls
            ]

        return _Response(
            choices=[
                _Choice(
                    message=_Message(content=content, tool_calls=tc_objects),
                    finish_reason=finish_reason,
                )
            ]
        )

    def test_text_response(self):
        response = self._make_response(content="Hello!", finish_reason="stop")
        result = OpenAICompatLLMProvider._parse_response(response)
        assert result.text == "Hello!"
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"
        assert result.raw_content == [{"type": "text", "text": "Hello!"}]

    def test_tool_call_response(self):
        response = self._make_response(
            content=None,
            tool_calls=[
                {"id": "call_1", "name": "list_benchmarks", "arguments": "{}"},
            ],
            finish_reason="tool_calls",
        )
        result = OpenAICompatLLMProvider._parse_response(response)
        assert result.text is None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_1"
        assert result.tool_calls[0].name == "list_benchmarks"
        assert result.tool_calls[0].input == {}
        assert result.stop_reason == "tool_use"

    def test_tool_call_raw_content_is_anthropic_format(self):
        """Verify raw_content uses Anthropic format for re-appending to messages."""
        response = self._make_response(
            content="Let me check.",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "check_host",
                    "arguments": '{"host": "10.0.0.1"}',
                },
            ],
            finish_reason="tool_calls",
        )
        result = OpenAICompatLLMProvider._parse_response(response)
        assert len(result.raw_content) == 2
        assert result.raw_content[0] == {"type": "text", "text": "Let me check."}
        assert result.raw_content[1]["type"] == "tool_use"
        assert result.raw_content[1]["id"] == "call_1"
        assert result.raw_content[1]["name"] == "check_host"
        assert result.raw_content[1]["input"] == {"host": "10.0.0.1"}

    def test_multiple_tool_calls(self):
        response = self._make_response(
            tool_calls=[
                {"id": "c1", "name": "tool_a", "arguments": '{"x": 1}'},
                {"id": "c2", "name": "tool_b", "arguments": '{"y": 2}'},
            ],
            finish_reason="tool_calls",
        )
        result = OpenAICompatLLMProvider._parse_response(response)
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].name == "tool_a"
        assert result.tool_calls[1].name == "tool_b"

    def test_malformed_arguments(self):
        response = self._make_response(
            tool_calls=[
                {"id": "c1", "name": "bad", "arguments": "not json"},
            ],
            finish_reason="tool_calls",
        )
        result = OpenAICompatLLMProvider._parse_response(response)
        assert result.tool_calls[0].input == {}


class TestConfigResolution:
    """Test per-agent model config resolution."""

    def test_default_config_non_builtin_agent(self):
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="claude", llm_model="claude-haiku-4-5")
        result = config.get_agent_llm_config("benchmark")
        assert result == {"provider": "claude", "model": "claude-haiku-4-5"}

    def test_builtin_defaults_apply(self):
        """Reasoning-heavy agents get Sonnet by default, others get global."""
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="claude", llm_model="claude-haiku-4-5")
        for agent in ("triage", "evaluating_convergence", "retrospective"):
            result = config.get_agent_llm_config(agent)
            assert result == {"provider": "claude", "model": "claude-sonnet-4-6"}, (
                f"{agent} should default to Sonnet"
            )
        result = config.get_agent_llm_config("benchmark")
        assert result["model"] == "claude-haiku-4-5", (
            "benchmark should use global default"
        )

    def test_builtin_defaults_inherit_provider(self):
        """Built-in defaults use the global provider, not a hardcoded one."""
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
        result = config.get_agent_llm_config("triage")
        assert result["provider"] == "gemini"
        assert result["model"] == "claude-sonnet-4-6"

    def test_explicit_config_overrides_builtin(self, tmp_path):
        """User's agent_models.<type> takes priority over built-in defaults."""
        import json

        from orchestrator.config import OrchestratorConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "agent_models": {
                        "triage": {
                            "provider": "anthropic",
                            "model": "claude-opus-4-8",
                        },
                    },
                }
            )
        )

        import orchestrator.config as cfg_mod

        original = cfg_mod.CONFIG_PATH
        cfg_mod.CONFIG_PATH = config_file
        try:
            config = OrchestratorConfig()
            assert config.get_agent_llm_config("triage")["model"] == "claude-opus-4-8"
            assert (
                config.get_agent_llm_config("evaluating_convergence")["model"]
                == "claude-sonnet-4-6"
            )
        finally:
            cfg_mod.CONFIG_PATH = original

    def test_default_config_overrides_builtin(self, tmp_path):
        """User's agent_models.default takes priority over built-in defaults."""
        import json

        from orchestrator.config import OrchestratorConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "agent_models": {
                        "default": {
                            "provider": "gemini",
                            "model": "gemini-2.5-flash",
                        },
                    },
                }
            )
        )

        import orchestrator.config as cfg_mod

        original = cfg_mod.CONFIG_PATH
        cfg_mod.CONFIG_PATH = config_file
        try:
            config = OrchestratorConfig()
            assert config.get_agent_llm_config("triage")["model"] == "gemini-2.5-flash"
            assert (
                config.get_agent_llm_config("retrospective")["model"]
                == "gemini-2.5-flash"
            )
        finally:
            cfg_mod.CONFIG_PATH = original

    def test_agent_specific_override(self, tmp_path):
        import json

        from orchestrator.config import OrchestratorConfig

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "llm": {"provider": "claude", "model": "claude-sonnet-4-6"},
                    "agent_models": {
                        "triage": {
                            "provider": "anthropic",
                            "model": "claude-haiku-4-5",
                        },
                        "review": {"provider": "anthropic", "model": "claude-opus-4-8"},
                        "default": {
                            "provider": "anthropic",
                            "model": "claude-sonnet-4-6",
                        },
                    },
                }
            )
        )

        import orchestrator.config as cfg_mod

        original = cfg_mod.CONFIG_PATH
        cfg_mod.CONFIG_PATH = config_file
        try:
            config = OrchestratorConfig()
            assert config.get_agent_llm_config("triage")["model"] == "claude-haiku-4-5"
            assert config.get_agent_llm_config("review")["model"] == "claude-opus-4-8"
            assert (
                config.get_agent_llm_config("benchmark")["model"] == "claude-sonnet-4-6"
            )
            assert (
                config.get_agent_llm_config("unknown")["model"] == "claude-sonnet-4-6"
            )
        finally:
            cfg_mod.CONFIG_PATH = original


class TestLLMFactory:
    """Test the LLM provider factory."""

    def test_mock_provider(self):
        from providers.llm.factory import create_llm_provider
        from providers.llm.mock import MockLLMProvider

        provider = create_llm_provider("mock")
        assert isinstance(provider, MockLLMProvider)

    def test_claude_provider(self):
        from providers.llm.claude import ClaudeLLMProvider
        from providers.llm.factory import create_llm_provider

        provider = create_llm_provider("claude", model="claude-sonnet-4-6")
        assert isinstance(provider, ClaudeLLMProvider)

    def test_anthropic_alias(self):
        from providers.llm.claude import ClaudeLLMProvider
        from providers.llm.factory import create_llm_provider

        provider = create_llm_provider("anthropic", model="claude-sonnet-4-6")
        assert isinstance(provider, ClaudeLLMProvider)

    def test_unknown_provider(self):
        from providers.llm.factory import create_llm_provider

        with pytest.raises(ValueError, match="Unknown"):
            create_llm_provider("unknown_provider")
