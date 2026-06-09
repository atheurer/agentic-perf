from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx

from providers.llm.base import (
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

logger = logging.getLogger(__name__)


class AgentBase(ABC):
    def __init__(
        self,
        agent_name: str,
        llm_provider: LLMProvider,
        state_store_url: str,
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.llm = llm_provider
        self.store_url = state_store_url.rstrip("/")
        self.tools = tools or []
        self._tool_handlers = tool_handlers or {}
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def run(self, ticket_id: str) -> None:
        logger.info(f"[{self.agent_name}] Starting on ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        messages = self._build_messages(ticket)
        max_iterations = 20

        for i in range(max_iterations):
            response = await self.llm.complete(
                system_prompt=self._system_prompt(),
                messages=messages,
                tools=self.tools if self.tools else None,
            )

            if response.stop_reason == "end_turn" or not response.tool_calls:
                await self._handle_completion(ticket_id, response)
                break

            submit_call = next(
                (tc for tc in response.tool_calls if tc.name.startswith("submit_")),
                None,
            )
            if submit_call:
                submit_response = LLMResponse(
                    text=None,
                    tool_calls=[submit_call],
                    stop_reason="tool_use",
                    raw_content=response.raw_content,
                )
                await self._handle_completion(ticket_id, submit_response)
                break

            messages.append({"role": "assistant", "content": response.raw_content})

            tool_results_content = []
            for tc in response.tool_calls:
                result = await self._execute_tool(tc)
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result.content,
                    "is_error": result.is_error,
                })

            messages.append({"role": "user", "content": tool_results_content})
        else:
            logger.warning(f"[{self.agent_name}] Hit max iterations on {ticket_id}")
            await self._add_comment(
                ticket_id,
                f"Agent {self.agent_name} reached maximum iteration limit.",
            )

        logger.info(f"[{self.agent_name}] Finished on ticket {ticket_id}")

    @abstractmethod
    def _system_prompt(self) -> str:
        ...

    @abstractmethod
    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None:
        ...

    @staticmethod
    def _parse_json_response(text: str | None) -> dict[str, Any]:
        if not text:
            return {}
        text = text.strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Extract JSON from markdown code fences
        import re
        fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Find the first { ... } block that parses as valid JSON
        brace_depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = None

        return {}

    @staticmethod
    def _get_submit_result(response: LLMResponse) -> dict[str, Any] | None:
        for tc in response.tool_calls:
            if tc.name.startswith("submit_"):
                return dict(tc.input)
        return None

    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        handler = self._tool_handlers.get(tool_call.name)
        if handler is None:
            return ToolResult(
                tool_use_id=tool_call.id,
                content=f"Unknown tool: {tool_call.name}",
                is_error=True,
            )
        try:
            result = await handler(**tool_call.input)
            if isinstance(result, str):
                content = result
            else:
                content = json.dumps(result, default=str)
            return ToolResult(tool_use_id=tool_call.id, content=content)
        except Exception as e:
            logger.exception(f"[{self.agent_name}] Tool {tool_call.name} failed")
            return ToolResult(
                tool_use_id=tool_call.id,
                content=f"Tool error: {e}",
                is_error=True,
            )

    async def _get_ticket(self, ticket_id: str) -> dict[str, Any]:
        r = await self._client.get(f"{self.store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        return r.json()

    async def _transition_ticket(
        self, ticket_id: str, new_status: str, comment: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"status": new_status}
        if comment:
            body["comment"] = comment
        r = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{ticket_id}/transition",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    async def _update_fields(
        self, ticket_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        r = await self._client.patch(
            f"{self.store_url}/api/v1/tickets/{ticket_id}/fields",
            json={"fields": fields},
        )
        r.raise_for_status()
        return r.json()

    async def _add_comment(self, ticket_id: str, body: str) -> dict[str, Any]:
        r = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{ticket_id}/comments",
            json={"author": self.agent_name, "body": body},
        )
        r.raise_for_status()
        return r.json()

    async def _request_human_input(
        self, ticket_id: str, question: str
    ) -> None:
        await self._add_comment(ticket_id, f"**Input needed:** {question}")
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=f"Agent {self.agent_name} needs clarification",
        )
