from __future__ import annotations

import asyncio
import os
from typing import Any

import anthropic

from .base import (
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    ToolCall,
    ToolDefinition,
)


class ClaudeLLMProvider(LLMProvider):
    _backend: str = "direct"

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

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = {
                "input_tokens": getattr(u, "input_tokens", 0) or 0,
                "output_tokens": getattr(u, "output_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0)
                or 0,
                "cache_creation_input_tokens": getattr(
                    u, "cache_creation_input_tokens", 0
                )
                or 0,
                "model": self._model,
            }

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            raw_content=raw_content,
            usage=usage,
        )

    def _stream_to_message(self, kwargs: dict[str, Any]) -> Any:
        """Run a completion via the streaming API and return the final message.

        Anthropic requires streaming for requests that may take longer than
        10 minutes to complete — a higher max_tokens combined with adaptive
        thinking (reasoning_effort) can cross that threshold, and a plain
        messages.create() call raises an APIError in that case. Streaming
        avoids the error; the caller only needs the final assembled message,
        not the incremental chunks, so this drains the stream synchronously.
        """
        with self._client.messages.stream(**kwargs) as stream:
            stream.until_done()
            return stream.get_final_message()

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._resolve_max_tokens(max_tokens),
            "system": system_prompt,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if self._backend == "vertex":
            kwargs.pop("cache_control", None)
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        if tools:
            kwargs["tools"] = [self._tool_def_to_dict(t) for t in tools]
        if self.reasoning_effort is not None:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.reasoning_effort}

        effective_timeout = self._resolve_timeout(timeout)
        if effective_timeout == 0:
            # Explicit 0 disables timeout.
            response = await asyncio.to_thread(self._stream_to_message, kwargs)
            return self._parse_response(response)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._stream_to_message, kwargs),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(effective_timeout, f"claude/{self._model}") from None
        except anthropic.RateLimitError as e:
            retry_after: float | None = None
            try:
                retry_after = float(e.response.headers.get("retry-after", 0)) or None
            except Exception:
                pass
            raise LLMRateLimitError(f"claude/{self._model}", retry_after) from None
        except anthropic.APIStatusError as e:
            # Vertex AI surfaces RESOURCE_EXHAUSTED as a non-429 APIStatusError.
            if "resource_exhausted" in str(e).lower():
                raise LLMRateLimitError(f"claude/{self._model}") from None
            raise

        return self._parse_response(response)
