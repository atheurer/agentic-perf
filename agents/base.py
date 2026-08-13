from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

import httpx

from providers.events import EventBus
from providers.llm.base import (
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

logger = logging.getLogger(__name__)


class HITLTimeoutError(Exception):
    """Raised when the HITL wait loop times out twice consecutively.

    The agent stops cleanly and leaves the ticket in awaiting_customer_guidance
    rather than returning a "proceed with best judgment" message to the LLM.
    """


class HITLDriftError(Exception):
    """Raised when the ticket moves away during HITL wait.

    The agent unwinds cleanly — no further transition needed.
    The orchestrator's exception handler must NOT transition the
    ticket to awaiting_customer_guidance; it is already where it
    should be (e.g., awaiting_teardown after an abort).
    """


class AgentAbortedError(Exception):
    """Raised when the agent detects the ticket has been aborted or drifted.

    Unlike HITLDriftError (raised inside HITL wait loops), this fires from
    the top-of-iteration drift check — catching status changes that happen
    between LLM calls rather than during them.
    """


class AgentBase(ABC):
    # Default inner-loop iteration budget. Agents can override
    # via constructor. Set to 0 for unlimited iterations —
    # termination is then driven by convergence gates, cost
    # guardrails (#127), or HITL intervention rather than an
    # arbitrary count.
    DEFAULT_MAX_ITERATIONS = 20

    # Global ticket-wide iteration ceiling. Configurable
    # via config.json global_max_iterations or env var
    # GLOBAL_MAX_ITERATIONS. The orchestrator sets this
    # on each agent; standalone usage keeps this default.
    DEFAULT_GLOBAL_MAX_ITERATIONS = 100

    # Minimum seconds between tool calls. Prevents agents
    # from overwhelming hosts with rapid-fire SSH commands
    # or API calls. Configurable via config.json:
    #   { "tool_rate_limit": { "min_interval_sec": 2.0 } }
    DEFAULT_TOOL_MIN_INTERVAL = 1.0

    # Number of automatic retries on LLM timeout before
    # escalating to awaiting_customer_guidance. Handles
    # transient API or network issues without requiring
    # human intervention.
    LLM_TIMEOUT_RETRIES = 2

    # Number of automatic retries on LLM rate limiting before
    # escalating to awaiting_customer_guidance. Rate limits are
    # transient so we retry more aggressively with exponential
    # backoff (min 15s, doubling each attempt, capped at 5m).
    LLM_RATE_LIMIT_RETRIES = 5

    def __init__(
        self,
        agent_name: str,
        llm_provider: LLMProvider,
        state_store_url: str,
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable] | None = None,
        event_bus: EventBus | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.llm = llm_provider
        self.store_url = state_store_url.rstrip("/")
        self.tools = tools or []
        self._tool_handlers = tool_handlers or {}
        self._mcp = None
        headers = {}
        api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)
        self._events = event_bus
        self._last_tool_call_time: float = 0.0
        self._tool_min_interval = self._load_tool_rate_limit()
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else self.DEFAULT_MAX_ITERATIONS
        )
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    async def close(self) -> None:
        await self._client.aclose()

    def _get_previous_iteration_counts(self, ticket_id: str) -> tuple[int, int]:
        """Count previous iterations (llm_request events) from the ticket's jsonl log file."""
        import json

        log_dir = self._events._log_dir if self._events else None
        if not log_dir:
            from paths import LOG_DIR

            log_dir = LOG_DIR
        path = log_dir / f"{ticket_id}.jsonl"
        if not path.exists():
            return 0, 0
        agent_iters = 0
        global_iters = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        if evt.get("event_type") == "llm_request":
                            global_iters += 1
                            if evt.get("agent") == self.agent_name:
                                agent_iters += 1
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"Failed to read previous iteration counts from {path}: {e}")
        return agent_iters, global_iters

    def _emit(
        self, ticket_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> None:
        if self._events:
            self._events.emit(ticket_id, self.agent_name, event_type, data)

    async def run(self, ticket_id: str) -> None:
        logger.info(f"[{self.agent_name}] Starting on ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        self._dispatched_status = ticket.get("status", "")
        self._aborted = False
        self._last_interject_ticket: dict[str, Any] | None = None
        system_prompt = self._system_prompt(ticket)
        cf = ticket.get("custom_fields", {})
        if cf.get("remember_previous") and cf.get("previous_messages"):
            messages = cf["previous_messages"]
            logger.info(
                f"[{self.agent_name}] Resuming with {len(messages)} previous messages"
            )
            await self._update_fields(
                ticket_id,
                {
                    "remember_previous": None,
                    "previous_messages": None,
                },
            )
        else:
            messages = self._build_messages(ticket)
        self._emit(
            ticket_id,
            "agent_started",
            {
                "system_prompt": system_prompt,
                "initial_messages": messages,
            },
        )
        try:
            try:
                previous_agent_iterations, previous_global_iterations = (
                    self._get_previous_iteration_counts(ticket_id)
                )
            except Exception:
                previous_agent_iterations, previous_global_iterations = 0, 0
            iteration = previous_agent_iterations

            if self.max_iterations > 0:
                remaining_agent = max(
                    0, self.max_iterations - previous_agent_iterations
                )
                global_max = cf.get(
                    "global_max_iterations_override", self.DEFAULT_GLOBAL_MAX_ITERATIONS
                )
                remaining_global = max(0, global_max - previous_global_iterations)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[SYSTEM] Resource limits: you have"
                            f" {self.max_iterations} total iterations allowed for this agent phase"
                            f" (already consumed: {previous_agent_iterations}, remaining: {remaining_agent})."
                            f" Global ticket iteration limit: {global_max}"
                            f" (already consumed across all agents: {previous_global_iterations}, remaining globally: {remaining_global})."
                            f" Plan your work to finish within these budgets."
                        ),
                    }
                )

            self._budget_grace = False
            self._iteration_grace = False
            while (
                self.max_iterations == 0
                or iteration < self.max_iterations
                or (self._iteration_grace and iteration == self.max_iterations)
            ):
                if self._stop_requested:
                    self._emit(
                        ticket_id,
                        "agent_stopped",
                        {"mode": "graceful"},
                    )
                    await self._transition_ticket(
                        ticket_id,
                        "awaiting_customer_guidance",
                        comment=("Agent stopped (graceful) by user request"),
                    )
                    break

                interject_msg = await self._check_interject(
                    ticket_id,
                )
                self._check_drift()
                if interject_msg:
                    messages.append(
                        {
                            "role": "user",
                            "content": (f"[USER INTERJECTION] {interject_msg}"),
                        }
                    )

                iteration += 1
                self._emit(
                    ticket_id,
                    "llm_request",
                    {"iteration": iteration - 1},
                )

                # Check global ticket max iterations
                global_max = cf.get(
                    "global_max_iterations_override", self.DEFAULT_GLOBAL_MAX_ITERATIONS
                )
                global_iterations = previous_global_iterations + (
                    iteration - previous_agent_iterations
                )
                if global_max > 0 and global_iterations > global_max:
                    logger.warning(
                        f"[{self.agent_name}] Hit global ticket max iterations"
                        f" ({global_max}) on {ticket_id}"
                    )
                    await self._save_messages(ticket_id, messages)
                    self._emit(
                        ticket_id,
                        "agent_error",
                        {"reason": "global_max_iterations"},
                    )
                    await self._add_comment(
                        ticket_id,
                        f"**Ticket reached global maximum iteration limit ({global_max}).**"
                        f" The execution across all agents has exhausted the global budget. Pausing for customer guidance.",
                    )
                    await self._transition_ticket(
                        ticket_id,
                        "awaiting_customer_guidance",
                        comment=f"Global iteration limit ({global_max}) reached",
                    )
                    break

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
                except LLMTimeoutError as e:
                    if tok is not None:
                        context.detach(tok)
                        tok = None
                    retries = getattr(self, "_llm_timeout_retries", 0)
                    self._llm_timeout_retries = retries + 1
                    if retries < self.LLM_TIMEOUT_RETRIES:
                        logger.warning(
                            f"[{self.agent_name}] LLM timeout"
                            f" on {ticket_id} (attempt"
                            f" {retries + 1}/"
                            f"{self.LLM_TIMEOUT_RETRIES}):"
                            f" {e} — retrying"
                        )
                        self._emit(
                            ticket_id,
                            "agent_error",
                            {
                                "reason": "llm_timeout",
                                "timeout_seconds": e.timeout,
                                "provider": e.provider,
                                "retry": retries + 1,
                                "max_retries": (self.LLM_TIMEOUT_RETRIES),
                            },
                        )
                        # Brief backoff before retry.
                        await asyncio.sleep(2**retries)
                        continue
                    logger.error(
                        f"[{self.agent_name}] LLM timeout"
                        f" on {ticket_id} after"
                        f" {self.LLM_TIMEOUT_RETRIES}"
                        f" retries: {e}"
                    )
                    self._emit(
                        ticket_id,
                        "agent_error",
                        {
                            "reason": "llm_timeout",
                            "timeout_seconds": e.timeout,
                            "provider": e.provider,
                            "retries_exhausted": True,
                        },
                    )
                    await self._add_comment(
                        ticket_id,
                        f"**Agent {self.agent_name} LLM call"
                        f" timed out** after {e.timeout}s"
                        f" ({e.provider})."
                        f" {self.LLM_TIMEOUT_RETRIES}"
                        f" automatic retries were"
                        f" attempted. This may indicate"
                        f" sustained API overload or a"
                        f" network issue. You can retry"
                        f" by replying here.",
                    )
                    await self._transition_ticket(
                        ticket_id,
                        "awaiting_customer_guidance",
                        comment=(
                            f"{self.agent_name} LLM call"
                            f" timed out after"
                            f" {self.LLM_TIMEOUT_RETRIES}"
                            f" retries — pausing for"
                            f" guidance"
                        ),
                    )
                    break
                except LLMRateLimitError as e:
                    if tok is not None:
                        context.detach(tok)
                        tok = None
                    retries = getattr(self, "_llm_rate_limit_retries", 0)
                    self._llm_rate_limit_retries = retries + 1
                    if retries < self.LLM_RATE_LIMIT_RETRIES:
                        wait = e.retry_after or min(15 * (2**retries), 300)
                        logger.warning(
                            f"[{self.agent_name}] Rate limited on"
                            f" {ticket_id} (attempt"
                            f" {retries + 1}/"
                            f"{self.LLM_RATE_LIMIT_RETRIES}):"
                            f" waiting {wait}s"
                        )
                        self._emit(
                            ticket_id,
                            "agent_error",
                            {
                                "reason": "rate_limited",
                                "provider": e.provider,
                                "wait_seconds": wait,
                                "retry": retries + 1,
                                "max_retries": self.LLM_RATE_LIMIT_RETRIES,
                            },
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.error(
                        f"[{self.agent_name}] Rate limit retries"
                        f" exhausted on {ticket_id} after"
                        f" {self.LLM_RATE_LIMIT_RETRIES} attempts"
                    )
                    self._emit(
                        ticket_id,
                        "agent_error",
                        {
                            "reason": "rate_limited",
                            "provider": e.provider,
                            "retries_exhausted": True,
                        },
                    )
                    await self._add_comment(
                        ticket_id,
                        f"**Agent {self.agent_name} hit sustained API rate"
                        f" limits** ({e.provider}) after"
                        f" {self.LLM_RATE_LIMIT_RETRIES} retries."
                        f" You can resume by replying here.",
                    )
                    await self._transition_ticket(
                        ticket_id,
                        "awaiting_customer_guidance",
                        comment=(
                            f"{self.agent_name} rate-limited after"
                            f" {self.LLM_RATE_LIMIT_RETRIES}"
                            f" retries — pausing for guidance"
                        ),
                    )
                    break
                finally:
                    if tok is not None:
                        context.detach(tok)
                self._llm_rate_limit_retries = 0
                self._emit(
                    ticket_id,
                    "llm_response",
                    {
                        "iteration": iteration - 1,
                        "stop_reason": response.stop_reason,
                        "tool_calls": [tc.name for tc in response.tool_calls],
                        "text_length": (len(response.text) if response.text else 0),
                        "text": response.text,
                        "raw_content": response.raw_content,
                    },
                )

                if response.usage and self._events:
                    self._events.record_llm_usage(
                        ticket_id=ticket_id,
                        input_tokens=response.usage.get("input_tokens", 0),
                        output_tokens=response.usage.get("output_tokens", 0),
                        duration_ms=0,
                        model=response.usage.get("model", ""),
                        agent_name=self.agent_name,
                        cache_read_input_tokens=response.usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        cache_creation_input_tokens=response.usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                    )
                    self._emit(
                        ticket_id,
                        "llm_usage",
                        response.usage,
                    )

                if self._events and iteration > 1:
                    budget_status = await self._check_budget(
                        ticket_id,
                    )
                    if budget_status == "pause":
                        # Inject a final system message so the
                        # LLM can wrap up gracefully before we
                        # cut it off on the next iteration.
                        if not getattr(self, "_budget_grace", False):
                            self._budget_grace = True
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM] Your token/cost "
                                        "budget for this ticket is "
                                        "exhausted. You MUST wrap up "
                                        "immediately: submit your "
                                        "best result now using your "
                                        "submit_* tool, even if "
                                        "incomplete. Summarize what "
                                        "was accomplished and what "
                                        "remains. This is your final "
                                        "LLM call."
                                    ),
                                }
                            )
                            continue  # one more LLM call
                        # Grace iteration used — hard stop.
                        await self._save_messages(ticket_id, messages)
                        await self._handle_budget_pause(ticket_id)
                        break
                    if budget_status == "warn":
                        # Soft limit: inform the LLM so it can
                        # start winding down proactively.
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[SYSTEM] Budget warning: you "
                                    "are approaching your token/cost "
                                    "limit for this ticket. Begin "
                                    "wrapping up your work. Finish "
                                    "any critical in-progress steps, "
                                    "then submit your results. Do "
                                    "not start new exploratory work."
                                ),
                            }
                        )

                if self.max_iterations > 0 and iteration > 1:
                    remaining = self.max_iterations - iteration
                    warn_at = max(1, self.max_iterations * 3 // 4)
                    if iteration == warn_at:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"[SYSTEM] Iteration warning:"
                                    f" you have used {iteration} of"
                                    f" {self.max_iterations}"
                                    f" iterations ({remaining}"
                                    f" remaining). Begin wrapping"
                                    f" up — finish critical work"
                                    f" and submit your results."
                                ),
                            }
                        )
                    elif remaining == 0:
                        if not self._iteration_grace:
                            self._iteration_grace = True
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM] This is your"
                                        " FINAL iteration. Submit"
                                        " your results NOW using"
                                        " your submit_* tool,"
                                        " even if incomplete."
                                    ),
                                }
                            )
                            continue

                if response.stop_reason == "end_turn" or not response.tool_calls:
                    logger.info(
                        f"[{self.agent_name}] end_turn/no_tools at iter "
                        f"{iteration}, stop_reason={response.stop_reason}, "
                        f"tool_calls={len(response.tool_calls or [])}"
                    )
                    has_submit_tool = any(
                        t.name.startswith("submit_") for t in (self.tools or [])
                    )
                    if has_submit_tool:
                        summary = (
                            response.text[:500]
                            if response.text
                            else "No explanation provided."
                        )
                        question = (
                            f"Agent **{self.agent_name}** could not"
                            f" complete its task and did not produce a"
                            f" structured result.\n\n"
                            f"**Agent's last message:**\n{summary}\n\n"
                            f"How would you like to proceed?"
                        )
                        self._emit(
                            ticket_id,
                            "escalation",
                            {"reason": "end_turn_without_submit"},
                        )
                        reply = await self._request_human_input(ticket_id, question)
                        messages.append(
                            {"role": "assistant", "content": response.raw_content}
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"The user has provided guidance:\n\n"
                                    f"{reply}\n\n"
                                    f"Please continue your work using this"
                                    f" feedback. When done, call your"
                                    f" submit tool with the results."
                                ),
                            }
                        )
                        continue
                    await self._handle_completion(ticket_id, response)
                    break

                submit_call = next(
                    (tc for tc in response.tool_calls if tc.name.startswith("submit_")),
                    None,
                )
                if submit_call:
                    logger.info(
                        f"[{self.agent_name}] submit_* call detected: "
                        f"{submit_call.name} (iter {iteration})"
                    )
                    block_msg = self._should_block_submit(ticket_id)
                    if block_msg:
                        self._emit(
                            ticket_id,
                            "tool_called",
                            {
                                "tool": submit_call.name,
                                "input_keys": list(submit_call.input.keys()),
                                "blocked": True,
                            },
                        )
                        messages.append(
                            {"role": "assistant", "content": response.raw_content}
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": submit_call.id,
                                        "content": block_msg,
                                        "is_error": True,
                                    }
                                ],
                            }
                        )
                        continue
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

                try:
                    pre_tool_ticket = await self._get_ticket(
                        ticket_id,
                    )
                    self._last_interject_ticket = pre_tool_ticket
                    self._check_drift()
                except AgentAbortedError:
                    raise
                except Exception:
                    pass

                tool_results_content = []
                for tc in calls_to_run:
                    if self._aborted:
                        raise AgentAbortedError(
                            "Skipping remaining tool calls — agent aborted"
                        )
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
                # while loop exhausted (max_iterations reached)
                await self._save_messages(ticket_id, messages)
                self._emit(
                    ticket_id,
                    "agent_error",
                    {"reason": "max_iterations"},
                )
                logger.warning(
                    f"[{self.agent_name}] Hit max iterations"
                    f" ({self.max_iterations}) on {ticket_id}"
                )
                await self._add_comment(
                    ticket_id,
                    f"**Agent {self.agent_name} reached maximum"
                    f" iteration limit ({self.max_iterations}).**"
                    f" The agent could not complete its work within"
                    f" the iteration budget. You can reply to guide"
                    f" next steps (e.g., retry, skip to review,"
                    f" or abort).",
                )
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment=(
                        f"{self.agent_name} hit max iterations — pausing for guidance"
                    ),
                )
        except (HITLDriftError, AgentAbortedError):
            raise
        except HITLTimeoutError as e:
            # Clean stop after two consecutive HITL timeouts.  The ticket is
            # already in awaiting_customer_guidance — just log and exit without
            # emitting an error event, since this is expected/intentional.
            logger.info(f"[{self.agent_name}] Stopped cleanly after HITL timeout: {e}")
            return
        except Exception as e:
            self._emit(ticket_id, "agent_error", {"reason": str(e)})
            raise

        self._emit(ticket_id, "agent_finished")
        logger.info(f"[{self.agent_name}] Finished on ticket {ticket_id}")

    @abstractmethod
    def _system_prompt(self, ticket: dict[str, Any]) -> str: ...

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

    def _should_block_submit(self, ticket_id: str) -> str | None:
        """Override to block submit_* calls. Return a rejection message
        string to block, or None to allow the submit to proceed."""
        return None

    @staticmethod
    def _get_submit_result(response: LLMResponse) -> dict[str, Any] | None:
        for tc in response.tool_calls:
            if tc.name.startswith("submit_"):
                return dict(tc.input)
        return None

    @staticmethod
    def _get_scoped_context(
        ticket: dict[str, Any],
        agent_key: str,
    ) -> str | None:
        """Return agent-scoped context, or None to fall back to full text."""
        cf = ticket.get("custom_fields", {})
        scoped = cf.get("scoped_context")
        if not scoped or not isinstance(scoped, dict):
            return None
        parts = []
        shared = scoped.get("shared")
        if shared:
            parts.append(shared)
        agent_section = scoped.get(agent_key)
        if agent_section:
            parts.append(agent_section)
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _load_prompt_fragments(
        agent_dir: Path,
        resource_provider: str | None = None,
        endpoint_type: str | None = None,
    ) -> str:
        """Load prompt fragments from the agent's prompts/ directory."""
        prompts_dir = agent_dir / "prompts"
        if not prompts_dir.is_dir():
            return ""

        parts = []
        if resource_provider:
            provider_file = prompts_dir / f"{resource_provider}.md"
            if provider_file.exists():
                parts.append(provider_file.read_text().strip())
        else:
            auto_file = prompts_dir / "auto_select.md"
            if auto_file.exists():
                parts.append(auto_file.read_text().strip())

        if endpoint_type:
            endpoint_file = prompts_dir / f"{endpoint_type}.md"
            if endpoint_file.exists():
                parts.append(endpoint_file.read_text().strip())

        return "\n\n".join(parts)

    def _load_tool_rate_limit(self) -> float:
        """Load tool rate limit from config."""
        try:
            from orchestrator.config import _load_config_file

            cfg = _load_config_file()
            return cfg.get("tool_rate_limit", {}).get(
                "min_interval_sec",
                self.DEFAULT_TOOL_MIN_INTERVAL,
            )
        except Exception:
            return self.DEFAULT_TOOL_MIN_INTERVAL

    async def _throttle_tool_call(self) -> None:
        """Enforce minimum interval between tool calls.

        Prevents agents from overwhelming hosts with rapid-fire
        SSH commands or API calls. Without this, an agent with
        max_iterations=0 can spawn hundreds of SSH subprocesses
        in seconds, crashing the target host.
        """
        if self._tool_min_interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_tool_call_time
        if elapsed < self._tool_min_interval:
            await asyncio.sleep(self._tool_min_interval - elapsed)
        self._last_tool_call_time = time.monotonic()

    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        await self._throttle_tool_call()
        handler = self._tool_handlers.get(tool_call.name)
        if handler is not None:
            try:
                result = await handler(**tool_call.input)
                if isinstance(result, str):
                    content = result
                else:
                    content = json.dumps(result, default=str)
                return ToolResult(tool_use_id=tool_call.id, content=content)
            except (HITLDriftError, HITLTimeoutError, AgentAbortedError):
                raise
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
            except (HITLDriftError, HITLTimeoutError, AgentAbortedError):
                raise
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

    async def _handle_budget_pause(self, ticket_id: str) -> None:
        """Handle budget pause during agent execution.

        Default: transition to awaiting_customer_guidance.
        Investigation agents should override to route to
        evaluating_convergence so partial results can be
        assessed.
        """
        await self._add_comment(
            ticket_id,
            f"**Agent {self.agent_name} paused: LLM "
            f"budget exhausted.**\n\n"
            f"The per-ticket token/cost budget has been "
            f"reached. Partial results may be available.",
        )
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=(f"{self.agent_name} budget exhausted — pausing for guidance"),
        )

    async def _check_budget(self, ticket_id: str) -> str:
        """Check per-ticket LLM budget.

        Returns 'ok', 'warn', or 'pause'. On 'pause', the agent
        transitions the ticket to awaiting_customer_guidance so
        the user can decide to increase the budget or abort.
        """
        try:
            from orchestrator.config import _load_config_file
            from providers.budget import (
                BudgetAction,
                budget_from_custom_fields,
                check_ticket_budget,
            )
            from providers.cost import estimate_cumulative_cost

            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            config = _load_config_file()
            budget = budget_from_custom_fields(cf, config)
            if budget is None:
                return "ok"

            assert self._events is not None
            usage = self._events.get_cumulative_usage(ticket_id)
            cost = estimate_cumulative_cost(usage)
            status = check_ticket_budget(budget, usage, cost)

            if status.action == BudgetAction.PAUSE:
                self._emit(
                    ticket_id,
                    "agent_error",
                    {
                        "reason": "budget_exceeded",
                        "detail": status.reason,
                    },
                )
                logger.warning(
                    f"[{self.agent_name}] Budget exceeded on"
                    f" {ticket_id}: {status.reason}"
                )
                await self._add_comment(
                    ticket_id,
                    f"**Budget exceeded:** {status.reason}\n\n"
                    f"Ticket paused. Increase the budget in "
                    f"custom_fields.llm_budget or approve "
                    f"continued spending.",
                )
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment=f"Budget exceeded: {status.reason}",
                )
                return "pause"

            if status.action == BudgetAction.WARN:
                logger.info(
                    f"[{self.agent_name}] Budget warning on"
                    f" {ticket_id}: {status.reason}"
                )
                await self._add_comment(
                    ticket_id,
                    f"**Budget warning:** {status.reason}",
                )
                return "warn"

        except ImportError:
            pass
        except Exception:
            logger.exception(f"[{self.agent_name}] Budget check failed")

        return "ok"

    async def _get_investigation_ledger(
        self,
        ticket_id: str,
    ) -> list[dict[str, Any]]:
        """Read the investigation ledger from the ticket."""
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        return cf.get("investigation_ledger", [])

    async def _append_ledger_entry(
        self,
        ticket_id: str,
        iteration: int,
        plan_steps: list[int] | None = None,
        hypothesis: str = "",
        params_rationale: str = "",
        conclusion: str = "",
        info_gain: float = 0.0,
    ) -> None:
        """Append an entry to the investigation ledger.

        Performs a read-modify-write on the ledger list.
        """
        from providers.ledger import LedgerEntry, append_ledger_entry

        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        entry = LedgerEntry(
            iteration=iteration,
            plan_steps=plan_steps or [],
            hypothesis=hypothesis,
            params_rationale=params_rationale,
            conclusion=conclusion,
            info_gain=info_gain,
        )
        fields = append_ledger_entry(cf, entry)
        await self._update_fields(ticket_id, fields)

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

    async def _save_messages(
        self,
        ticket_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        try:
            await self._update_fields(
                ticket_id,
                {"previous_messages": messages},
            )
        except Exception:
            logger.debug(f"Failed to save messages for {ticket_id}")

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

    _PLAN_AGENT_STATUS = {
        "teardown": "awaiting_teardown",
        "resource": "awaiting_hardware",
        "provision": "awaiting_provision",
        "benchmark": "executing_benchmark",
        "review": "awaiting_review",
        "analyze": "analyzing",
        "synthesis": "synthesizing_results",
    }

    async def _plan_controls_next_transition(self, ticket_id: str) -> bool:
        """Check whether the execution plan controls the next transition.

        Returns True only when:
        1. A plan exists with more steps after the current one, AND
        2. The current step is "in_progress" (matching _advance_plan), AND
        3. The current step's agent_type matches this agent (i.e., this
           agent is executing a plan-managed step, not a pre-plan step
           in the normal pipeline).

        This prevents pre-plan agents (e.g., the initial resource/provision
        cycle before step 0) from deferring their transitions.
        """
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        plan = cf.get("execution_plan")
        if not plan:
            return False
        steps = plan.get("steps", [])
        current_idx = plan.get("current_step", 0)
        if current_idx >= len(steps) or current_idx + 1 >= len(steps):
            return False
        step = steps[current_idx]
        if step.get("status") != "in_progress":
            return False
        expected_status = self._PLAN_AGENT_STATUS.get(
            step.get("agent_type", ""),
        )
        ticket_status = ticket.get("status", "")
        return expected_status == ticket_status

    def _check_drift(self) -> None:
        """Raise AgentAbortedError if ticket status drifted from dispatch.

        Uses the ticket cached by _check_interject — no extra HTTP call.
        Exempts awaiting_customer_guidance (budget-grace transitions there).
        """
        if self._aborted:
            raise AgentAbortedError("Agent already aborted")
        ticket = self._last_interject_ticket
        if ticket is None:
            return
        current_status = ticket.get("status", "")
        if current_status == self._dispatched_status:
            return
        if current_status == "awaiting_customer_guidance":
            return
        self._aborted = True
        self._emit(
            ticket.get("id", ""),
            "agent_aborted",
            {
                "reason": "status_drift",
                "dispatched_status": self._dispatched_status,
                "current_status": current_status,
            },
        )
        raise AgentAbortedError(
            f"Ticket drifted from {self._dispatched_status} to {current_status}"
        )

    async def _check_interject(self, ticket_id: str) -> str | None:
        """Check for and consume a pending user interjection.

        Returns the interjection message if one was queued,
        otherwise None. Clears the field after pickup so the
        same interjection is never delivered twice.
        """
        try:
            ticket = await self._get_ticket(ticket_id)
        except Exception:
            return None
        self._last_interject_ticket = ticket
        cf = ticket.get("custom_fields", {})
        interject = cf.get("pending_interject")
        if not interject:
            return None
        message = interject.get("message", "")
        await self._update_fields(
            ticket_id,
            {"pending_interject": None},
        )
        self._emit(
            ticket_id,
            "user_interjection",
            {"message": message},
        )
        return message

    _HITL_POLL_INTERVAL = 5.0
    _HITL_TIMEOUT = 1800.0
    _HITL_NO_RESUME_STATUSES = frozenset(
        {
            "awaiting_teardown",
            "retrospective_pending",
            "closed",
        }
    )

    async def _request_human_input(self, ticket_id: str, question: str) -> str:
        """Pause for human input and return the user's reply.

        Transitions to awaiting_customer_guidance, polls until the
        user replies (ticket leaves that status), then returns the
        reply text. The agent's LLM loop continues with full context.
        """
        ticket = await self._get_ticket(ticket_id)
        comment_count = len(ticket.get("comments", []))
        await self._add_comment(ticket_id, f"**Input needed:** {question}")
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=f"Agent {self.agent_name} needs clarification",
        )

        logger.info(f"[{self.agent_name}] Waiting for human input on {ticket_id}")
        directives = ticket.get("custom_fields", {}).get("directives", {})
        timeout = (
            float("inf")
            if directives.get("disable_hitl_timeout")
            else self._HITL_TIMEOUT
        )
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(self._HITL_POLL_INTERVAL)
            elapsed += self._HITL_POLL_INTERVAL
            ticket = await self._get_ticket(ticket_id)
            if ticket.get("status") != "awaiting_customer_guidance":
                resumed_status = ticket.get("status", "")
                cf = ticket.get("custom_fields", {})
                if resumed_status in self._HITL_NO_RESUME_STATUSES or cf.get(
                    "abort_requested"
                ):
                    self._emit(
                        ticket_id,
                        "agent_aborted",
                        {
                            "reason": "ticket_drifted",
                            "new_status": resumed_status,
                            "abort_requested": bool(
                                cf.get("abort_requested"),
                            ),
                        },
                    )
                    raise HITLDriftError(
                        f"Ticket {ticket_id} moved to "
                        f"{resumed_status} while agent "
                        f"{self.agent_name} was waiting"
                    )
                self._emit(
                    ticket_id,
                    "hitl_resumed",
                    {
                        "to": resumed_status,
                        "comment": "Resumed after user reply",
                        "ticket_id": ticket_id,
                    },
                )
                new_comments = ticket.get("comments", [])[comment_count:]
                user_replies = [
                    c["body"]
                    for c in new_comments
                    if c.get("author") not in ("system", self.agent_name)
                ]
                reply = (
                    "\n".join(user_replies) if user_replies else "User resumed ticket."
                )
                logger.info(f"[{self.agent_name}] Human input received on {ticket_id}")

                # Intercept slash commands before returning to the LLM loop
                if reply.strip().startswith("/"):
                    handled = await self._handle_slash_command(ticket_id, reply.strip())
                    if handled is not None:
                        return handled

                return reply

        logger.warning(f"[{self.agent_name}] HITL timeout on {ticket_id}")

        # Track consecutive timeouts on this agent instance.  On the first
        # timeout we re-ask and keep waiting; on the second we stop entirely
        # and leave the ticket paused for human input.  This prevents the LLM
        # from autonomously "proceeding with best judgment" when the user has
        # not responded — which leads to unguided analysis and wasted iterations.
        self._hitl_timeout_count = getattr(self, "_hitl_timeout_count", 0) + 1

        if self._hitl_timeout_count == 1:
            # First timeout: add a warning comment and re-enter the wait loop
            # for one more full timeout period.
            logger.info(
                f"[{self.agent_name}] First HITL timeout on {ticket_id} "
                "— re-asking and waiting one more period"
            )
            await self._add_comment(
                ticket_id,
                "⏱️ No response received within the timeout window. "
                "Still waiting for your guidance — please reply to continue. "
                "The agent will pause permanently if there is no response.",
            )
            elapsed = 0.0
            while elapsed < timeout:
                await asyncio.sleep(self._HITL_POLL_INTERVAL)
                elapsed += self._HITL_POLL_INTERVAL
                ticket = await self._get_ticket(ticket_id)
                if ticket.get("status") != "awaiting_customer_guidance":
                    resumed_status = ticket.get("status", "")
                    cf = ticket.get("custom_fields", {})
                    if resumed_status in self._HITL_NO_RESUME_STATUSES or cf.get(
                        "abort_requested"
                    ):
                        self._emit(
                            ticket_id,
                            "agent_aborted",
                            {
                                "reason": "ticket_drifted",
                                "new_status": resumed_status,
                                "abort_requested": bool(
                                    cf.get("abort_requested"),
                                ),
                            },
                        )
                        raise HITLDriftError(
                            f"Ticket {ticket_id} moved to "
                            f"{resumed_status} while agent "
                            f"{self.agent_name} was waiting"
                        )
                    new_comments = ticket.get("comments", [])[comment_count:]
                    user_replies = [
                        c["body"]
                        for c in new_comments
                        if c.get("author") not in ("system", self.agent_name)
                    ]
                    reply = (
                        "\n".join(user_replies)
                        if user_replies
                        else "User resumed ticket."
                    )
                    logger.info(
                        f"[{self.agent_name}] Human input received after re-ask "
                        f"on {ticket_id}"
                    )
                    if reply.strip().startswith("/"):
                        handled = await self._handle_slash_command(
                            ticket_id, reply.strip()
                        )
                        if handled is not None:
                            return handled
                    self._hitl_timeout_count = 0
                    return reply

            # Second timeout — fall through to the permanent-pause path below
            self._hitl_timeout_count += 1

        # Second (or subsequent) consecutive timeout: stop the agent and leave
        # the ticket in awaiting_customer_guidance.  Raise an exception so the
        # LLM loop exits cleanly without receiving a "proceed" message.
        logger.warning(
            f"[{self.agent_name}] Second HITL timeout on {ticket_id} "
            "— stopping agent, ticket remains paused for human input"
        )
        await self._add_comment(
            ticket_id,
            "⏸️ No response received after two timeout periods. "
            "The agent has stopped and is waiting for your reply. "
            "Please respond to resume the investigation.",
        )
        raise HITLTimeoutError(
            f"Agent {self.agent_name} stopped after two consecutive HITL "
            f"timeouts on {ticket_id}. Ticket remains paused for human input."
        )

    async def _handle_slash_command(self, ticket_id: str, command: str) -> str | None:
        """Handle a slash command issued by the user during HITL.

        Called when _request_human_input() receives a reply starting with '/'.
        The return value is passed directly back to the LLM as the HITL reply;
        return None to fall through and pass the raw command text to the LLM.

        Subclasses should override to add agent-specific commands.  Always call
        super() first so generic handling and the unknown-command guard fire.

        Generic commands (/abort, /model, /extend-iterations) are fully handled
        by the CLI before the agent sees them and will never reach here.
        """
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()

        # Known agent-specific commands not handled here (subclass responsibility)
        _agent_specific = {"/submit"}
        if cmd in _agent_specific:
            return None  # let subclass handle it

        # Reject any other unrecognised slash command so it doesn't confuse the LLM
        known = {"/abort", "/close", "/model", "/extend-iterations"} | _agent_specific
        if cmd not in known:
            logger.warning(f"[{self.agent_name}] Unknown slash command: {cmd}")
            return (
                f"ERROR: '{cmd}' is not a recognised slash command. "
                f"Available commands: /abort, /close, /model <id>, "
                f"/extend-iterations <n>, /submit (review only). "
                f"Please send a normal message or a valid command."
            )

        return None
