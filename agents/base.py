from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx

from providers.events import EventBus
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
        event_bus: EventBus | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.llm = llm_provider
        self.store_url = state_store_url.rstrip("/")
        self.tools = tools or []
        self._tool_handlers = tool_handlers or {}
        self._mcp = None
        self._client = httpx.AsyncClient(timeout=30.0)
        self._events = event_bus

    async def close(self) -> None:
        await self._client.aclose()

    def _emit(
        self, ticket_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> None:
        if self._events:
            self._events.emit(ticket_id, self.agent_name, event_type, data)

    async def run(self, ticket_id: str) -> None:
        logger.info(f"[{self.agent_name}] Starting on ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        system_prompt = self._system_prompt()
        messages = self._build_messages(ticket)
        self._emit(
            ticket_id,
            "agent_started",
            {
                "system_prompt": system_prompt,
                "initial_messages": messages,
            },
        )
        max_iterations = 20

        try:
            for i in range(max_iterations):
                self._emit(
                    ticket_id,
                    "llm_request",
                    {"iteration": i},
                )

                # Set ticket context for OTLP span
                # correlation so the span processor
                # can attribute token usage to this
                # ticket.
                tok = None
                try:
                    from opentelemetry import context

                    from providers.telemetry import (
                        set_ticket_context,
                    )

                    tok = context.attach(
                        set_ticket_context(
                            ticket_id,
                            self.agent_name,
                        )
                    )
                except ImportError:
                    pass

                try:
                    response = await self.llm.complete(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=(self.tools if self.tools else None),
                    )
                finally:
                    if tok is not None:
                        context.detach(tok)
                self._emit(
                    ticket_id,
                    "llm_response",
                    {
                        "iteration": i,
                        "stop_reason": response.stop_reason,
                        "tool_calls": [tc.name for tc in response.tool_calls],
                        "text_length": (len(response.text) if response.text else 0),
                        "text": response.text,
                        "raw_content": response.raw_content,
                    },
                )

                if response.stop_reason == "end_turn" or not response.tool_calls:
                    await self._handle_completion(ticket_id, response)
                    break

                submit_call = next(
                    (tc for tc in response.tool_calls if tc.name.startswith("submit_")),
                    None,
                )
                if submit_call:
                    self._emit(
                        ticket_id,
                        "tool_called",
                        {
                            "tool": submit_call.name,
                            "input_keys": list(submit_call.input.keys()),
                            "input": submit_call.input,
                        },
                    )
                    submit_response = LLMResponse(
                        text=None,
                        tool_calls=[submit_call],
                        stop_reason="tool_use",
                        raw_content=response.raw_content,
                    )
                    await self._handle_completion(ticket_id, submit_response)
                    break

                messages.append({"role": "assistant", "content": response.raw_content})

                calls_to_run = response.tool_calls
                if len(calls_to_run) > 1:
                    non_clarify = [
                        tc for tc in calls_to_run if tc.name != "request_clarification"
                    ]
                    if non_clarify:
                        skipped = [tc for tc in calls_to_run if tc not in non_clarify]
                        for tc in skipped:
                            self._emit(
                                ticket_id,
                                "tool_skipped",
                                {
                                    "tool": tc.name,
                                    "reason": "other tools executed first",
                                },
                            )
                        calls_to_run = non_clarify

                tool_results_content = []
                for tc in calls_to_run:
                    self._emit(
                        ticket_id,
                        "tool_called",
                        {
                            "tool": tc.name,
                            "input_keys": list(tc.input.keys()),
                            "input": tc.input,
                        },
                    )
                    result = await self._execute_tool(tc)
                    self._emit(
                        ticket_id,
                        "tool_result",
                        {
                            "tool": tc.name,
                            "is_error": result.is_error,
                            "content_length": len(result.content),
                            "content": result.content,
                        },
                    )
                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
                for tc in response.tool_calls:
                    if tc not in calls_to_run:
                        tool_results_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": "Skipped: other tools executed first",
                                "is_error": False,
                            }
                        )

                messages.append({"role": "user", "content": tool_results_content})
            else:
                self._emit(ticket_id, "agent_error", {"reason": "max_iterations"})
                logger.warning(f"[{self.agent_name}] Hit max iterations on {ticket_id}")
                await self._add_comment(
                    ticket_id,
                    f"Agent {self.agent_name} reached maximum iteration limit.",
                )
        except Exception as e:
            self._emit(ticket_id, "agent_error", {"reason": str(e)})
            raise

        self._emit(ticket_id, "agent_finished")
        logger.info(f"[{self.agent_name}] Finished on ticket {ticket_id}")

    @abstractmethod
    def _system_prompt(self) -> str: ...

    @abstractmethod
    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None: ...

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
        if handler is not None:
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

        if self._mcp is not None:
            try:
                content = await self._mcp.call_tool(tool_call.name, tool_call.input)
                return ToolResult(tool_use_id=tool_call.id, content=content)
            except Exception as e:
                logger.exception(
                    f"[{self.agent_name}] MCP tool {tool_call.name} failed"
                )
                return ToolResult(
                    tool_use_id=tool_call.id,
                    content=f"Tool error: {e}",
                    is_error=True,
                )

        return ToolResult(
            tool_use_id=tool_call.id,
            content=f"Unknown tool: {tool_call.name}",
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
        self._emit(ticket_id, "comment", {"body": body[:200]})
        r = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{ticket_id}/comments",
            json={"author": self.agent_name, "body": body},
        )
        r.raise_for_status()
        return r.json()

    async def _request_human_input(self, ticket_id: str, question: str) -> None:
        await self._add_comment(ticket_id, f"**Input needed:** {question}")
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=f"Agent {self.agent_name} needs clarification",
        )
