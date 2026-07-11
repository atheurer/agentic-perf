"""OpenAI-compatible LLM provider.

Supports OpenAI, Azure OpenAI, and any endpoint that implements the
OpenAI Chat Completions API (vLLM, Ollama, LiteLLM, etc.) via base_url.

Messages throughout agentic-perf use Anthropic-native format. This provider
converts at the boundary: Anthropic→OpenAI on the way in, OpenAI→Anthropic
on the way out. No changes to AgentBase or agent code required.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .base import (
    LLMProvider,
    LLMResponse,
    LLMTimeoutError,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


class OpenAICompatLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        base_url: str | None = None,
    ) -> None:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "The 'openai' package is required for the OpenAI provider. "
                "Install it with: pip install openai"
            )

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        self._model = model

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse:
        oai_messages = self._convert_messages(system_prompt, messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        effective_timeout = self._resolve_timeout(timeout)
        if effective_timeout == 0:
            response = await asyncio.to_thread(
                self._client.chat.completions.create, **kwargs
            )
            return self._parse_response(response)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._client.chat.completions.create, **kwargs),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(effective_timeout, f"openai/{self._model}") from None
        return self._parse_response(response)

    @staticmethod
    def _convert_messages(
        system_prompt: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        oai: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "user" and isinstance(content, list):
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        tool_content = tr.get("content", "")
                        if tr.get("is_error"):
                            tool_content = f"Error: {tool_content}"
                        oai.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr["tool_use_id"],
                                "content": tool_content,
                            }
                        )
                    continue

            if role == "assistant" and isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            }
                        )

                assistant_msg: dict[str, Any] = {"role": "assistant"}
                text = "\n".join(text_parts) if text_parts else None
                assistant_msg["content"] = text
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                oai.append(assistant_msg)
                continue

            oai.append({"role": role, "content": content})

        return oai

    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(response) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        text = message.content
        tool_calls = []
        raw_content: list[dict[str, Any]] = []

        if text:
            raw_content.append({"type": "text", "text": text})

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        input=arguments,
                    )
                )
                raw_content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": arguments,
                    }
                )

        finish_reason = choice.finish_reason
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = finish_reason or "end_turn"

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = {
                "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "model": "",
            }

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=raw_content,
            usage=usage,
        )
