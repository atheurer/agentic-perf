from __future__ import annotations

import asyncio
import os
from typing import Any

import anthropic

from .base import LLMProvider, LLMResponse, ToolCall, ToolDefinition


class ClaudeLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-haiku-4-5",
        backend: str | None = None,
        project_id: str | None = None,
        region: str | None = None,
    ) -> None:
        self._model = model

        backend = backend or os.environ.get("LLM_BACKEND", "auto")
        if backend == "auto":
            if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or os.environ.get(
                "CLAUDE_CODE_USE_VERTEX"
            ):
                backend = "vertex"
            elif os.environ.get("ANTHROPIC_API_KEY"):
                backend = "direct"
            else:
                backend = "vertex"

        if backend == "vertex":
            self._client = anthropic.AnthropicVertex(
                project_id=project_id or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"),
                region=region or os.environ.get("CLOUD_ML_REGION", "us-east5"),
            )
        else:
            self._client = anthropic.Anthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            )

        self._backend = backend

    def _tool_def_to_dict(self, tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        text_parts = []
        tool_calls = []
        raw_content = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input)
                )
                raw_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            raw_content=raw_content,
        )

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = [self._tool_def_to_dict(t) for t in tools]

        response = await asyncio.to_thread(self._client.messages.create, **kwargs)
        return self._parse_response(response)
