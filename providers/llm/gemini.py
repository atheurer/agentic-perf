"""Google Gemini LLM provider.

Supports the Gemini Developer API (API key) and Vertex AI via the
unified google-genai SDK.

Messages throughout agentic-perf use Anthropic-native format. This provider
converts at the boundary: Anthropic→Gemini on the way in, Gemini→Anthropic
on the way out. No changes to AgentBase or agent code required.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

from .base import (
    LLMProvider,
    LLMResponse,
    LLMTimeoutError,
    ToolCall,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


def _resolve_json_refs(obj: Any) -> Any:
    """Inline JSON Schema $ref definitions so Gemini doesn't
    misinterpret '#/definitions/...' as internal part references.

    Walks the entire tree looking for dicts that contain a
    'definitions' or '$defs' key, then resolves any '$ref'
    pointers within that subtree.
    """
    if not isinstance(obj, dict) and not isinstance(obj, list):
        return obj

    def _resolve_subtree(root: dict) -> dict:
        defs = root.get("definitions") or root.get("$defs") or {}

        def _resolve(node: Any) -> Any:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/"):
                    path = ref.lstrip("#/").split("/")
                    target: Any = root
                    for p in path:
                        if isinstance(target, dict):
                            target = target.get(p)
                        else:
                            return node
                    if target is not None:
                        return _resolve(target)
                    return node
                skip = ("definitions", "$defs")
                return {k: _resolve(v) for k, v in node.items() if k not in skip}
            if isinstance(node, list):
                return [_resolve(item) for item in node]
            return node

        if defs:
            return _resolve(root)
        return root

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            has_defs = "definitions" in node or "$defs" in node
            if has_defs:
                node = _resolve_subtree(node)
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(obj)


class GeminiLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        project_id: str | None = None,
        region: str | None = None,
    ) -> None:
        try:
            from google import genai
            from google.genai import types  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'google-genai' package is required for the Gemini provider. "
                "Install it with: pip install google-genai"
            )

        api_key = (
            api_key
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        region = region or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

        if project_id and not api_key:
            self._client = genai.Client(
                vertexai=True,
                project=project_id,
                location=region,
            )
        else:
            self._client = genai.Client(api_key=api_key)

        self._model = model

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> LLMResponse:
        from google.genai import types

        contents, tool_call_names = self._convert_messages(messages)

        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "max_output_tokens": max_tokens,
        }
        if tools:
            config_kwargs["tools"] = [self._convert_tools(tools)]
            config_kwargs["automatic_function_calling"] = (
                types.AutomaticFunctionCallingConfig(disable=True)
            )

        effective_timeout = self._resolve_timeout(timeout)
        if effective_timeout == 0:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return self._parse_response(response, tool_call_names)
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                ),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(effective_timeout, f"gemini/{self._model}") from None
        return self._parse_response(response, tool_call_names)

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[Any], dict[str, str]]:
        """Convert Anthropic-native messages to Gemini Content objects.

        Returns (contents, tool_call_names) where tool_call_names maps
        synthetic tool_use IDs to function names for tool_result conversion.
        """
        from google.genai import types

        contents: list[types.Content] = []
        tool_call_names: dict[str, str] = {}

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "assistant" and isinstance(content, list):
                parts: list[types.Part] = []
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            parts.append(types.Part.from_text(text=text))
                    elif block.get("type") == "tool_use":
                        tool_id = block.get("id", "")
                        name = block["name"]
                        tool_call_names[tool_id] = name
                        fc_part = types.Part(
                            function_call=types.FunctionCall(
                                name=name,
                                args=block.get("input", {}),
                            ),
                        )
                        ts = block.get("thought_signature")
                        if ts is not None:
                            fc_part.thought_signature = base64.b64decode(ts)
                        parts.append(fc_part)
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            if role == "user" and isinstance(content, list):
                tool_results = [b for b in content if b.get("type") == "tool_result"]
                if tool_results:
                    parts = []
                    for tr in tool_results:
                        tool_use_id = tr.get("tool_use_id", "")
                        name = tool_call_names.get(tool_use_id, "unknown")
                        result_content = tr.get("content", "")
                        if tr.get("is_error"):
                            result_content = f"Error: {result_content}"
                        try:
                            parsed = json.loads(result_content)
                            result_dict = (
                                parsed
                                if isinstance(parsed, dict)
                                else {"result": parsed}
                            )
                        except (json.JSONDecodeError, TypeError):
                            result_dict = {"result": result_content}
                        result_dict = _resolve_json_refs(result_dict)
                        parts.append(
                            types.Part.from_function_response(
                                name=name,
                                response=result_dict,
                            )
                        )
                    contents.append(types.Content(role="tool", parts=parts))
                    continue

                text_parts = [
                    b.get("text", "") for b in content if b.get("type") == "text"
                ]
                text = "\n".join(text_parts) if text_parts else str(content)
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=text)],
                    )
                )
                continue

            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=str(content or ""))],
                )
            )

        return contents, tool_call_names

    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> Any:
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.input_schema,
            )
            for t in tools
        ]
        return types.Tool(function_declarations=declarations)

    @staticmethod
    def _parse_response(
        response: Any,
        tool_call_names: dict[str, str],
    ) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_content: list[dict[str, Any]] = []

        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.content and candidate.content.parts:
            for i, part in enumerate(candidate.content.parts):
                if part.text:
                    text_parts.append(part.text)
                    raw_content.append({"type": "text", "text": part.text})
                elif part.function_call:
                    fc = part.function_call
                    fc_id = fc.id or f"gemini_fc_{i}"
                    name = fc.name or "unknown"
                    args = dict(fc.args) if fc.args else {}
                    tool_call_names[fc_id] = name
                    tool_calls.append(ToolCall(id=fc_id, name=name, input=args))
                    block: dict[str, Any] = {
                        "type": "tool_use",
                        "id": fc_id,
                        "name": name,
                        "input": args,
                    }
                    ts = getattr(part, "thought_signature", None)
                    if ts is not None:
                        block["thought_signature"] = base64.b64encode(
                            ts if isinstance(ts, bytes) else ts.encode()
                        ).decode("ascii")
                    raw_content.append(block)

        if tool_calls:
            stop_reason = "tool_use"
        else:
            finish = candidate.finish_reason if candidate else None
            finish_str = finish.value if hasattr(finish, "value") else str(finish or "")
            if finish_str == "MAX_TOKENS":
                stop_reason = "max_tokens"
            else:
                stop_reason = "end_turn"

        usage = None
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            usage = {
                "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                "cache_read_input_tokens": getattr(um, "cached_content_token_count", 0)
                or 0,
                "cache_creation_input_tokens": 0,
                "model": "",
            }

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_content=raw_content,
            usage=usage,
        )
