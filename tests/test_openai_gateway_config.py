"""Exercise an OpenAI-compatible gateway through the configured LLM provider."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.main import _make_llm_factory, _resolve_api_key_secret
from providers.llm.base import ToolDefinition


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
async def test_gateway_config_and_function_tool_round_trip(
    api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured gateway, secret, model, and API reach the SDK together."""
    for variable in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API",
    ):
        monkeypatch.delenv(variable, raising=False)

    model = "rits/deepseek-ai/DeepSeek-V4.1-Flash-1017"
    config = OrchestratorConfig(
        raw_config={
            "llm": {
                "provider": "openai",
                "model": model,
                "base_url": "https://gateway.example.test/v1",
                "api": api,
                "api_key_secret": "gateway/api-key",
            }
        }
    )
    secrets = MagicMock()
    secrets.get_secret = AsyncMock(return_value="gateway-test-key")
    key = await _resolve_api_key_secret(config, "benchmark", secrets)
    secrets.get_secret.assert_awaited_once_with("gateway/api-key")

    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if api == "chat_completions":
            message = (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "report_check",
                                "arguments": '{"value":"OK"}',
                            },
                        }
                    ],
                }
                if len(requests) == 1
                else {"role": "assistant", "content": "OK"}
            )
            data = {
                "id": "chat_1",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
            }
        else:
            item = (
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "report_check",
                    "arguments": '{"value":"OK"}',
                    "status": "completed",
                }
                if len(requests) == 1
                else {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "OK", "annotations": []}
                    ],
                }
            )
            data = {
                "id": "resp_1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": model,
                "output": [item],
            }
        return httpx.Response(200, json=data)

    original_client = openai.OpenAI
    with httpx.Client(transport=httpx.MockTransport(handle_request)) as http_client:
        with patch(
            "openai.OpenAI",
            side_effect=lambda **kwargs: original_client(
                http_client=http_client, **kwargs
            ),
        ):
            provider = _make_llm_factory(config, api_keys={"benchmark": key})(
                "benchmark"
            )

        tools = [
            ToolDefinition(
                "report_check",
                "Report a check value",
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ]
        messages = [{"role": "user", "content": "Check the value."}]
        first = await provider.complete("Use the tool.", messages, tools=tools)
        assert first.stop_reason == "tool_use"
        assert first.tool_calls[0].name == "report_check"
        assert first.tool_calls[0].input == {"value": "OK"}
        messages.extend(
            [
                {"role": "assistant", "content": first.raw_content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": first.tool_calls[0].id,
                            "content": "OK",
                        }
                    ],
                },
            ]
        )
        second = await provider.complete("Use the tool.", messages, tools=tools)

    assert second.text == "OK"
    assert second.stop_reason == "end_turn"
    assert len(requests) == 2
    path = "/v1/chat/completions" if api == "chat_completions" else "/v1/responses"
    for request in requests:
        assert str(request.url) == f"https://gateway.example.test{path}"
        assert request.headers["authorization"] == "Bearer gateway-test-key"
        assert json.loads(request.content)["model"] == model

    second_body = json.loads(requests[1].content)
    if api == "chat_completions":
        assert second_body["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "OK",
        }
    else:
        assert second_body["input"][-1] == {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "OK",
        }
