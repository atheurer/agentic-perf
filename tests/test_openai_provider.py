"""Unit tests for the OpenAI-compatible LLM provider.

Tests message conversion, tool conversion, and response parsing using
mock data — no live API calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_responses_tools_use_flat_function_schema(self):
        tool = ToolDefinition(
            name="check_host",
            description="Check a host",
            input_schema={"type": "object"},
        )
        assert OpenAICompatLLMProvider._convert_responses_tools([tool]) == [
            {
                "type": "function",
                "name": "check_host",
                "description": "Check a host",
                "parameters": {"type": "object"},
            }
        ]

    def test_responses_input_converts_tool_turn(self):
        result = OpenAICompatLLMProvider._convert_responses_input(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "check_host",
                            "input": {"host": "host1"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "ok",
                        }
                    ],
                },
            ]
        )
        assert result == [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "check_host",
                "arguments": '{"host": "host1"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ]


class TestResponseParsing:
    """Test OpenAI response → LLMResponse conversion."""

    def _make_response(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        finish_reason: str = "stop",
        usage: dict | None = None,
        model: str | None = None,
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
        class _TokenDetails:
            cached_tokens: int = 0
            cache_write_tokens: int = 0

        @dataclass
        class _Usage:
            prompt_tokens: int = 0
            completion_tokens: int = 0
            prompt_tokens_details: _TokenDetails | None = None

        @dataclass
        class _Response:
            choices: list = field(default_factory=list)
            usage: _Usage | None = None
            model: str | None = None

        tc_objects = None
        if tool_calls:
            tc_objects = [
                _ToolCall(
                    id=tc["id"],
                    function=_Function(name=tc["name"], arguments=tc["arguments"]),
                )
                for tc in tool_calls
            ]

        usage_obj = None
        if usage:
            usage_obj = _Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                prompt_tokens_details=_TokenDetails(
                    cached_tokens=usage.get("cached_tokens", 0),
                    cache_write_tokens=usage.get("cache_write_tokens", 0),
                ),
            )

        return _Response(
            choices=[
                _Choice(
                    message=_Message(content=content, tool_calls=tc_objects),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage_obj,
            model=model,
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

    def test_usage_parsing_with_response_model(self):
        response = self._make_response(
            content="Hello!",
            usage={"prompt_tokens": 120, "completion_tokens": 45},
            model="gpt-4o-2024-05-13",
        )
        result = OpenAICompatLLMProvider._parse_response(response, model="gpt-4o")
        assert result.usage is not None
        assert result.usage["input_tokens"] == 120

    def test_chat_usage_includes_cache_details(self):
        response = self._make_response(
            content="Hello!",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 70,
                "cache_write_tokens": 5,
            },
        )
        result = OpenAICompatLLMProvider._parse_response(response)
        assert result.usage["cache_read_input_tokens"] == 70
        assert result.usage["cache_creation_input_tokens"] == 5

    def test_responses_response_parsing(self):
        response = SimpleNamespace(
            model="gpt-5.6-luna",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_1",
                    name="check_host",
                    arguments='{"host": "host1"}',
                )
            ],
            usage=SimpleNamespace(input_tokens=12, output_tokens=8),
        )
        result = OpenAICompatLLMProvider._parse_responses_response(response)
        assert result.text is None
        assert result.stop_reason == "tool_use"
        assert result.tool_calls[0].id == "call_1"
        assert result.tool_calls[0].input == {"host": "host1"}
        assert result.usage["output_tokens"] == 8

    def test_responses_usage_includes_cache_details(self):
        response = SimpleNamespace(
            output=[],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=70,
                    cache_write_tokens=5,
                ),
            ),
        )
        result = OpenAICompatLLMProvider._parse_responses_response(response)
        assert result.usage["cache_read_input_tokens"] == 70
        assert result.usage["cache_creation_input_tokens"] == 5

    def test_responses_text_parsing(self):
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="Hello!")],
                )
            ],
            usage=None,
        )
        result = OpenAICompatLLMProvider._parse_responses_response(response)
        assert result.text == "Hello!"
        assert result.stop_reason == "end_turn"

    def test_usage_parsing_fallback_to_provider_model(self):
        response = self._make_response(
            content="Hello!",
            usage={"prompt_tokens": 80, "completion_tokens": 30},
            model=None,
        )
        result = OpenAICompatLLMProvider._parse_response(response, model="gpt-4o-mini")
        assert result.usage is not None
        assert result.usage["input_tokens"] == 80
        assert result.usage["output_tokens"] == 30
        assert result.usage["model"] == "gpt-4o-mini"

    def test_usage_none_when_not_provided(self):
        response = self._make_response(content="Hello!", usage=None)
        result = OpenAICompatLLMProvider._parse_response(response, model="gpt-4o")
        assert result.usage is None


class TestResponsesAPI:
    def test_complete_uses_responses_endpoint_and_parameters(self):
        provider = OpenAICompatLLMProvider.__new__(OpenAICompatLLMProvider)
        provider._api = "responses"
        provider._model = "gpt-5.6-luna"
        provider.default_timeout = None
        provider.reasoning_effort = "medium"
        provider.max_tokens = None
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="ready")],
                )
            ],
            usage=None,
        )
        client = MagicMock()
        client.responses.create.return_value = response
        provider._client = client

        import asyncio

        async def run_in_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", new=run_in_thread):
            result = asyncio.run(
                provider.complete(
                    system_prompt="system",
                    messages=[{"role": "user", "content": "hello"}],
                    timeout=0,
                )
            )

        kwargs = client.responses.create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.6-luna"
        assert kwargs["instructions"] == "system"
        assert kwargs["max_output_tokens"] == 8000
        assert kwargs["reasoning"] == {"effort": "medium"}
        assert result.text == "ready"
        client.chat.completions.create.assert_not_called()


class TestConfigResolution:
    """Test per-agent model config resolution."""

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_default_config_non_builtin_agent(self, _mock_cfg):
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="claude", llm_model="claude-haiku-4-5")
        result = config.get_agent_llm_config("benchmark")
        assert result == {"provider": "claude", "model": "claude-haiku-4-5"}

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_global_model_applies_everywhere(self, _mock_cfg):
        """All agents get the global model — no builtin overrides."""
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="claude", llm_model="claude-haiku-4-5")
        for agent in ("triage", "evaluating_convergence", "retrospective"):
            result = config.get_agent_llm_config(agent)
            assert result["model"] == "claude-haiku-4-5", (
                f"{agent} should use global model"
            )
        result = config.get_agent_llm_config("benchmark")
        assert result["model"] == "claude-haiku-4-5", (
            "benchmark should use global default"
        )

    @patch("orchestrator.config._load_config_file", return_value={})
    def test_global_model_applies_to_all_agents(self, _mock_cfg):
        """Global model is used for all agents — no builtin overrides."""
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
        result = config.get_agent_llm_config("triage")
        assert result["provider"] == "gemini"
        assert result["model"] == "gemini-2.5-flash"

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
            # Other agents get the global default (empty
            # since no llm.model was set in config)
            assert config.get_agent_llm_config("evaluating_convergence")["model"] == ""
        finally:
            cfg_mod.CONFIG_PATH = original

    def test_default_config_ignored(self, tmp_path):
        """agent_models.default is no longer applied."""
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
            # agent_models.default is ignored — agents get
            # the global default (empty, no llm.model set)
            assert config.get_agent_llm_config("triage")["model"] == ""
            assert config.get_agent_llm_config("retrospective")["model"] == ""
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
