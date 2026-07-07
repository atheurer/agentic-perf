"""Unit tests for the Gemini LLM provider.

Tests message conversion, tool conversion, and response parsing using
mock data — no live API calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from providers.llm.base import ToolDefinition
from providers.llm.gemini import GeminiLLMProvider

# --- Mock Gemini SDK types for response parsing tests ---


@dataclass
class _FunctionCall:
    name: str | None = None
    args: dict[str, Any] | None = None
    id: str | None = None


@dataclass
class _Part:
    text: str | None = None
    function_call: _FunctionCall | None = None


@dataclass
class _Content:
    role: str = "model"
    parts: list[_Part] | None = None


@dataclass
class _FinishReason:
    value: str = "STOP"


@dataclass
class _Candidate:
    content: _Content | None = None
    finish_reason: _FinishReason = field(default_factory=_FinishReason)


@dataclass
class _Response:
    candidates: list[_Candidate] | None = None


class TestMessageConversion:
    """Test Anthropic-native → Gemini Content conversion."""

    def test_simple_user_message(self):
        contents, names = GeminiLLMProvider._convert_messages(
            [{"role": "user", "content": "hello"}]
        )
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "hello"

    def test_assistant_text_only(self):
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I'll help you."}],
                },
            ]
        )
        assert len(contents) == 1
        assert contents[0].role == "model"
        assert contents[0].parts[0].text == "I'll help you."

    def test_assistant_with_tool_calls(self):
        contents, names = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "list_benchmarks",
                            "input": {"category": "network"},
                        },
                    ],
                },
            ]
        )
        assert len(contents) == 1
        assert contents[0].role == "model"
        assert len(contents[0].parts) == 2
        assert contents[0].parts[0].text == "Let me check."
        fc_part = contents[0].parts[1]
        assert fc_part.function_call.name == "list_benchmarks"
        assert fc_part.function_call.args == {"category": "network"}
        assert names["tc_1"] == "list_benchmarks"

    def test_tool_result_becomes_function_response(self):
        contents, names = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "check_host",
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
                            "content": '{"status": "ok"}',
                            "is_error": False,
                        },
                    ],
                },
            ]
        )
        assert len(contents) == 2
        assert contents[1].role == "tool"
        fr_part = contents[1].parts[0]
        assert fr_part.function_response.name == "check_host"
        assert fr_part.function_response.response == {"status": "ok"}

    def test_tool_result_error_prefixed(self):
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "check_host",
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
                            "content": "Not found",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        fr_part = contents[1].parts[0]
        assert fr_part.function_response.response == {"result": "Error: Not found"}

    def test_tool_result_non_json_content(self):
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "run_cmd",
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
                            "content": "plain text output",
                            "is_error": False,
                        },
                    ],
                },
            ]
        )
        fr_part = contents[1].parts[0]
        assert fr_part.function_response.response == {"result": "plain text output"}

    def test_tool_result_name_lookup_unknown(self):
        """tool_result with no preceding tool_use falls back to 'unknown'."""
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan_id",
                            "content": "data",
                            "is_error": False,
                        },
                    ],
                },
            ]
        )
        assert contents[0].parts[0].function_response.name == "unknown"

    def test_multiple_tool_results(self):
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tc_1",
                            "name": "tool_a",
                            "input": {},
                        },
                        {
                            "type": "tool_use",
                            "id": "tc_2",
                            "name": "tool_b",
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
                            "content": "result_a",
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tc_2",
                            "content": "result_b",
                            "is_error": False,
                        },
                    ],
                },
            ]
        )
        assert len(contents[1].parts) == 2
        assert contents[1].parts[0].function_response.name == "tool_a"
        assert contents[1].parts[1].function_response.name == "tool_b"

    def test_user_text_content_list(self):
        """User message with a content list containing text blocks."""
        contents, _ = GeminiLLMProvider._convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First part"},
                        {"type": "text", "text": "Second part"},
                    ],
                },
            ]
        )
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert "First part" in contents[0].parts[0].text

    def test_full_conversation_roundtrip(self):
        contents, names = GeminiLLMProvider._convert_messages(
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
            ]
        )
        assert len(contents) == 4
        assert contents[0].role == "user"
        assert contents[1].role == "model"
        assert contents[1].parts[1].function_call.name == "list_benchmarks"
        assert contents[2].role == "tool"
        assert contents[2].parts[0].function_response.name == "list_benchmarks"
        assert contents[3].role == "model"
        assert contents[3].parts[0].text == "Found uperf. Submitting."
        assert names["tc_1"] == "list_benchmarks"


class TestToolConversion:
    """Test ToolDefinition → Gemini FunctionDeclaration conversion."""

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
        result = GeminiLLMProvider._convert_tools(tools)
        assert len(result.function_declarations) == 1
        fd = result.function_declarations[0]
        assert fd.name == "check_host"
        assert fd.description == "Check a host"
        assert fd.parameters_json_schema["type"] == "object"
        assert "host" in fd.parameters_json_schema["properties"]

    def test_multiple_tools(self):
        tools = [
            ToolDefinition(
                name="a", description="tool a", input_schema={"type": "object"}
            ),
            ToolDefinition(
                name="b", description="tool b", input_schema={"type": "object"}
            ),
        ]
        result = GeminiLLMProvider._convert_tools(tools)
        assert len(result.function_declarations) == 2
        assert result.function_declarations[0].name == "a"
        assert result.function_declarations[1].name == "b"


class TestResponseParsing:
    """Test Gemini response → LLMResponse conversion."""

    def test_text_response(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[_Part(text="Hello!")],
                    ),
                    finish_reason=_FinishReason("STOP"),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.text == "Hello!"
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"
        assert result.raw_content == [{"type": "text", "text": "Hello!"}]

    def test_function_call_response(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(
                                function_call=_FunctionCall(
                                    name="list_benchmarks",
                                    args={"category": "network"},
                                )
                            ),
                        ],
                    ),
                    finish_reason=_FinishReason("STOP"),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.text is None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "list_benchmarks"
        assert result.tool_calls[0].input == {"category": "network"}
        assert result.stop_reason == "tool_use"

    def test_function_call_id_synthesis(self):
        """Gemini returns None for FunctionCall.id; provider synthesizes one."""
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(
                                function_call=_FunctionCall(
                                    name="check_host", args={}, id=None
                                )
                            ),
                        ],
                    ),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.tool_calls[0].id == "gemini_fc_0"

    def test_function_call_preserves_sdk_id(self):
        """If the SDK provides an ID, use it."""
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(
                                function_call=_FunctionCall(
                                    name="tool_a", args={}, id="sdk_id_1"
                                )
                            ),
                        ],
                    ),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.tool_calls[0].id == "sdk_id_1"

    def test_raw_content_is_anthropic_format(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(text="Let me check."),
                            _Part(
                                function_call=_FunctionCall(
                                    name="check_host",
                                    args={"host": "10.0.0.1"},
                                )
                            ),
                        ],
                    ),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert len(result.raw_content) == 2
        assert result.raw_content[0] == {"type": "text", "text": "Let me check."}
        assert result.raw_content[1]["type"] == "tool_use"
        assert result.raw_content[1]["name"] == "check_host"
        assert result.raw_content[1]["input"] == {"host": "10.0.0.1"}

    def test_multiple_function_calls(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(
                                function_call=_FunctionCall(
                                    name="tool_a", args={"x": 1}
                                )
                            ),
                            _Part(
                                function_call=_FunctionCall(
                                    name="tool_b", args={"y": 2}
                                )
                            ),
                        ],
                    ),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].name == "tool_a"
        assert result.tool_calls[1].name == "tool_b"
        assert result.stop_reason == "tool_use"

    def test_max_tokens_finish_reason(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[_Part(text="Truncated output")],
                    ),
                    finish_reason=_FinishReason("MAX_TOKENS"),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.stop_reason == "max_tokens"

    def test_empty_response(self):
        response = _Response(candidates=[_Candidate(content=None)])
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.text is None
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"

    def test_no_candidates(self):
        response = _Response(candidates=[])
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.text is None
        assert result.tool_calls == []

    def test_function_call_with_none_args(self):
        response = _Response(
            candidates=[
                _Candidate(
                    content=_Content(
                        role="model",
                        parts=[
                            _Part(
                                function_call=_FunctionCall(
                                    name="no_args_tool", args=None
                                )
                            ),
                        ],
                    ),
                )
            ]
        )
        result = GeminiLLMProvider._parse_response(response, {})
        assert result.tool_calls[0].input == {}


class TestLLMFactory:
    """Test that the factory creates GeminiLLMProvider."""

    def test_gemini_provider(self):
        from providers.llm.factory import create_llm_provider
        from providers.llm.gemini import GeminiLLMProvider

        provider = create_llm_provider(
            "gemini", model="gemini-2.5-flash", api_key="test-key"
        )
        assert isinstance(provider, GeminiLLMProvider)

    def test_google_alias(self):
        from providers.llm.factory import create_llm_provider
        from providers.llm.gemini import GeminiLLMProvider

        provider = create_llm_provider(
            "google", model="gemini-2.5-pro", api_key="test-key"
        )
        assert isinstance(provider, GeminiLLMProvider)
