"""OpenAI-compatible LLM provider.

Supports OpenAI, Azure OpenAI, and any endpoint that implements the
OpenAI Chat Completions API (vLLM, Ollama, LiteLLM, etc.) via base_url.
The native OpenAI Responses API can be selected explicitly for endpoints
that support it.

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
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


class OpenAICompatLLMProvider(LLMProvider):
    @staticmethod
    def _uses_max_completion_tokens(model: str) -> bool:
        """Return whether a model uses the newer completion-token parameter.

        OpenAI reasoning-model families reject the legacy ``max_tokens``
        parameter. Keep the legacy spelling for older OpenAI-compatible
        endpoints such as vLLM and Ollama.
        """
        normalized = model.lower()
        return normalized.startswith(("gpt-5", "o1", "o3", "o4"))

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        base_url: str | None = None,
        api: str = "chat_completions",
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
        if api not in ("chat_completions", "responses"):
            raise ValueError(
                f"OpenAI API must be 'chat_completions' or 'responses', got {api!r}"
            )
        self._api = api

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        if getattr(self, "_api", "chat_completions") == "responses":
            return await self._complete_responses(
                system_prompt, messages, tools, max_tokens, timeout
            )

        oai_messages = self._convert_messages(system_prompt, messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
        }
        token_limit = self._resolve_max_tokens(max_tokens)
        token_parameter = (
            "max_completion_tokens"
            if self._uses_max_completion_tokens(self._model)
            else "max_tokens"
        )
        kwargs[token_parameter] = token_limit
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort

        effective_timeout = self._resolve_timeout(timeout)
        if effective_timeout == 0:
            response = await asyncio.to_thread(
                self._client.chat.completions.create, **kwargs
            )
            return self._parse_response(response, model=self._model)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._client.chat.completions.create, **kwargs),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(effective_timeout, f"openai/{self._model}") from None
        except Exception as e:
            # openai.RateLimitError is only available if the package is installed;
            # check by name to avoid a hard import dependency here.
            if type(e).__name__ == "RateLimitError":
                raise LLMRateLimitError(f"openai/{self._model}") from None
            raise
        return self._parse_response(response, model=self._model)

    async def _complete_responses(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None,
        max_tokens: int | None,
        timeout: float | None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": system_prompt,
            "input": self._convert_responses_input(messages),
            "max_output_tokens": self._resolve_max_tokens(max_tokens),
        }
        if tools:
            kwargs["tools"] = self._convert_responses_tools(tools)
        if self.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}

        effective_timeout = self._resolve_timeout(timeout)
        request = self._client.responses.create
        if effective_timeout == 0:
            response = await asyncio.to_thread(request, **kwargs)
            return self._parse_responses_response(response, model=self._model)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(request, **kwargs), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(effective_timeout, f"openai/{self._model}") from None
        except Exception as e:
            if type(e).__name__ == "RateLimitError":
                raise LLMRateLimitError(f"openai/{self._model}") from None
            raise
        return self._parse_responses_response(response, model=self._model)

    @staticmethod
    def _parse_cache_details(details: Any) -> tuple[int, int]:
        """Return cached and cache-written input tokens from provider usage."""
        if details is None:
            return 0, 0

        def value(name: str) -> int:
            raw = (
                details.get(name, 0)
                if isinstance(details, dict)
                else getattr(details, name, 0)
            )
            return int(raw or 0)

        return value("cached_tokens"), value("cache_write_tokens")

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
    def _convert_responses_input(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert agentic-perf messages to Responses API input items."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "user" and isinstance(content, list):
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        output = tr.get("content", "")
                        if tr.get("is_error"):
                            output = f"Error: {output}"
                        result.append(
                            {
                                "type": "function_call_output",
                                "call_id": tr["tool_use_id"],
                                "output": str(output),
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
                                "type": "function_call",
                                "call_id": block["id"],
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            }
                        )
                if text_parts:
                    result.append(
                        {"role": "assistant", "content": "\n".join(text_parts)}
                    )
                result.extend(tool_calls)
                continue

            result.append({"role": role, "content": content})
        return result

    @staticmethod
    def _convert_responses_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(response, model: str = "") -> LLMResponse:
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
            prompt_t = getattr(u, "prompt_tokens", 0) or 0
            cache_read, cache_write = OpenAICompatLLMProvider._parse_cache_details(
                getattr(u, "prompt_tokens_details", None)
            )
            usage = {
                "input_tokens": prompt_t,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "context_tokens": prompt_t,
                "model": getattr(response, "model", None) or model,
            }

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=raw_content,
            usage=usage,
        )

    @staticmethod
    def _parse_responses_response(response, model: str = "") -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_content: list[dict[str, Any]] = []

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for content in getattr(item, "content", []) or []:
                    if getattr(content, "type", None) == "output_text":
                        text = getattr(content, "text", "")
                        if text:
                            text_parts.append(text)
                            raw_content.append({"type": "text", "text": text})
            elif item_type == "function_call":
                arguments_text = getattr(item, "arguments", "") or ""
                try:
                    arguments = json.loads(arguments_text)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                name = getattr(item, "name", "")
                tool_calls.append(ToolCall(id=call_id, name=name, input=arguments))
                raw_content.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": arguments,
                    }
                )

        usage = None
        response_usage = getattr(response, "usage", None)
        if response_usage:
            input_tokens = getattr(response_usage, "input_tokens", 0) or 0
            cache_read, cache_write = OpenAICompatLLMProvider._parse_cache_details(
                getattr(response_usage, "input_tokens_details", None)
            )
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": getattr(response_usage, "output_tokens", 0) or 0,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "context_tokens": input_tokens,
                "model": getattr(response, "model", None) or model,
            }

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            raw_content=raw_content,
            usage=usage,
        )
