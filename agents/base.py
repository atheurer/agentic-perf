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

    # Default maximum tool response size (in bytes) before spilling
    # to the ticket scratchpad workspace to optimize LLM tokens.
    DEFAULT_TOOL_SPILL_THRESHOLD = 4096

    # Class-level default for mock spec compatibility
    _max_iterations_is_override = False

    def __init__(
        self,
        agent_name: str,
        llm_provider: LLMProvider,
        state_store_url: str,
        tools: list[ToolDefinition] | None = None,
        tool_handlers: dict[str, Callable] | None = None,
        event_bus: EventBus | None = None,
        max_iterations: int | None = None,
        tool_spill_threshold: int | None = None,
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
        self._max_iterations_is_override = False
        self._current_ticket_id: str | None = None
        self._tool_call_seq: int = 0
        self._spill_threshold: int = (
            tool_spill_threshold
            if tool_spill_threshold is not None
            else self._load_tool_spill_threshold()
        )
        self._register_workspace_tools()

    def _register_workspace_tools(self) -> None:
        """Register native workspace tools on the agent."""
        from agents.workspace.tools import WORKSPACE_TOOLS

        def _get_manager():
            from providers.workspace.manager import WorkspaceManager

            return WorkspaceManager(ticket_id=self._current_ticket_id)

        async def _jq_query(file_ref: str, filter: str, limit: int = 50) -> str:
            res = _get_manager().jq_query(file_ref, filter, limit=limit)
            return json.dumps(res, indent=2)

        async def _grep_file(
            file_ref: str,
            pattern: str,
            max_lines: int = 50,
            context_lines: int = 0,
            case_insensitive: bool = True,
        ) -> str:
            res = _get_manager().grep_file(
                file_ref,
                pattern,
                max_lines=max_lines,
                context_lines=context_lines,
                case_insensitive=case_insensitive,
            )
            return json.dumps(res, indent=2)

        async def _read_file_slice(
            file_ref: str,
            offset_bytes: int = 0,
            max_bytes: int = 4096,
            start_line: int | None = None,
            max_lines: int | None = None,
        ) -> str:
            res = _get_manager().read_file_slice(
                file_ref,
                offset_bytes=offset_bytes,
                max_bytes=max_bytes,
                start_line=start_line,
                max_lines=max_lines,
            )
            return json.dumps(res, indent=2)

        async def _list_workspace_files() -> str:
            res = _get_manager().list_files()
            return json.dumps(res, indent=2)

        ws_handlers = {
            "jq_query": _jq_query,
            "grep_file": _grep_file,
            "read_file_slice": _read_file_slice,
            "list_workspace_files": _list_workspace_files,
        }

        for name, handler in ws_handlers.items():
            if name not in self._tool_handlers:
                self._tool_handlers[name] = handler

        existing_names = {t.name for t in self.tools}
        for tool_def in WORKSPACE_TOOLS:
            if tool_def.name not in existing_names:
                self.tools.append(tool_def)

    def request_stop(self) -> None:
        self._stop_requested = True

    async def close(self) -> None:
        await self._client.aclose()

    def _get_previous_iteration_counts(self, ticket_id: str) -> tuple[int, int]:
        """Count previous iterations (llm_request events) from the ticket's jsonl log file.

        For fleet investigations, only counts iterations AFTER
        the last ``fleet_iteration_epoch`` event. This resets
        the per-agent budget each fleet iteration so agents
        don't exhaust their budget across the full fleet.
        """
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
                        if evt.get("event_type") == "fleet_iteration_epoch":
                            # Reset counts — new fleet iteration
                            agent_iters = 0
                            global_iters = 0
                        elif evt.get("event_type") == "llm_request":
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
        self._current_ticket_id = ticket_id
        logger.info(f"[{self.agent_name}] Starting on ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        self._dispatched_status = ticket.get("status", "")
        self._aborted = False
        self._last_interject_ticket: dict[str, Any] | None = None
        system_prompt = self._system_prompt(ticket)
        cf = ticket.get("custom_fields", {})
        spill_threshold = getattr(
            self, "_spill_threshold", getattr(self, "DEFAULT_SPILL_THRESHOLD", 4096)
        )
        if "tool_spill_threshold" in cf and cf["tool_spill_threshold"] is not None:
            try:
                spill_threshold = int(cf["tool_spill_threshold"])
                self._spill_threshold = spill_threshold
            except (ValueError, TypeError):
                pass
        workspace_prompt = (
            "\n\n## Scratchpad Workspace & Tool Querying\n"
            f"- **Automatic Spilling**: Tool outputs exceeding {spill_threshold} bytes are automatically saved "
            "to your ticket workspace (e.g. `workspace://tool_name_1.json`). Use `jq_query` to query JSON fields, "
            "`read_file_slice` to paginate text/logs, and `grep_file` to search.\n"
            "- **In-flight `jq_filter` parameter**: You can pass `jq_filter` (or `jq_query`) directly in ANY JSON-returning "
            "tool call (e.g., `cdm_api_request`, `get_hardware_topology`, `get_tool_params`, `get_ethtool_info`) "
            "to slice and return the exact data in a single turn without multi-step querying."
        )
        if "## Scratchpad Workspace" not in system_prompt:
            system_prompt = f"{system_prompt}{workspace_prompt}"
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
        configured_max = self.max_iterations
        try:
            try:
                previous_agent_iterations, previous_global_iterations = (
                    self._get_previous_iteration_counts(ticket_id)
                )
            except Exception:
                previous_agent_iterations, previous_global_iterations = 0, 0
            iteration = previous_agent_iterations

            # Override is additive: grant N new iterations on
            # top of what was already consumed in prior runs.
            if (
                self._max_iterations_is_override
                and previous_agent_iterations > 0
                and configured_max > 0
            ):
                self.max_iterations = configured_max + previous_agent_iterations
                logger.info(
                    f"[{self.agent_name}] Additive override:"
                    f" {configured_max} new iterations"
                    f" (effective limit {self.max_iterations})"
                )

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

            self._wrapup_reason: str | None = None
            self._context_warned = False
            while (
                self.max_iterations == 0
                or iteration < self.max_iterations
                or (
                    self._wrapup_reason is not None and iteration == self.max_iterations
                )
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

                # --- Context guard (checked first) ---
                if (
                    response.usage
                    and self._events
                    and iteration >= 1
                    and self._wrapup_reason is None
                ):
                    ctx_action = await self._check_context(
                        ticket_id,
                        response.usage,
                    )
                    if ctx_action == "pause":
                        if self._wrapup_reason is None:
                            self._wrapup_reason = "context"
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM] Your context window "
                                        "is nearly full. You MUST wrap "
                                        "up immediately: submit your "
                                        "best result now using your "
                                        "submit_* tool, even if "
                                        "incomplete. Raising the token "
                                        "budget will NOT help — this "
                                        "is a model input-size limit. "
                                        "This is your final LLM call."
                                    ),
                                }
                            )
                            continue
                    elif ctx_action == "warn":
                        if not getattr(self, "_context_warned", False):
                            self._context_warned = True
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "[SYSTEM] Context warning: "
                                        "your conversation is "
                                        "approaching the model's "
                                        "context window limit. Begin "
                                        "wrapping up your work. "
                                        "Finish critical in-progress "
                                        "steps, then submit results."
                                    ),
                                }
                            )

                # Context grace used — hard stop (before budget
                # so the right handler fires).
                if self._wrapup_reason == "context":
                    await self._save_messages(ticket_id, messages)
                    await self._handle_context_pause(ticket_id)
                    break

                # --- Budget guard ---
                if self._events and iteration > 1:
                    budget_status = await self._check_budget(
                        ticket_id,
                    )
                    if budget_status == "pause":
                        if self._wrapup_reason is None:
                            self._wrapup_reason = "budget"
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
                            continue
                        # Grace iteration used — hard stop.
                        await self._save_messages(ticket_id, messages)
                        await self._handle_budget_pause(ticket_id)
                        break
                    if budget_status == "warn":
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

                # --- Iteration guard ---
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
                        if self._wrapup_reason is None:
                            self._wrapup_reason = "iteration"
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
        finally:
            self.max_iterations = configured_max
            self._max_iterations_is_override = False

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
        """Return agent-scoped context, or None to fall back to full text.

        When verbatim_directives exist for the agent key, they are injected
        first under an authoritative header, followed by any triage-generated
        supplemental context. Without verbatim directives the legacy plain-
        text format is preserved so existing tests are unaffected.
        """
        cf = ticket.get("custom_fields", {})
        verbatim_directives = cf.get("verbatim_directives") or {}
        verbatim = verbatim_directives.get(agent_key, "")

        scoped = cf.get("scoped_context")
        has_scoped = bool(scoped and isinstance(scoped, dict))
        shared = (scoped.get("shared") if has_scoped else None) or ""
        supplemental = (scoped.get(agent_key) if has_scoped else None) or ""

        if not verbatim:
            # Legacy path: plain concatenation, no headers.
            parts = []
            if shared:
                parts.append(shared)
            if supplemental:
                parts.append(supplemental)
            return "\n\n".join(parts) if parts else None

        # Verbatim path: inject authoritative block first.
        parts = []
        if shared:
            parts.append(shared)
        parts.append(f"## Directives (authoritative — follow exactly):\n{verbatim}")
        if supplemental:
            parts.append(f"## Additional context:\n{supplemental}")
        return "\n\n".join(parts)

    @staticmethod
    def _user_comments(ticket: dict[str, Any]) -> list[dict[str, Any]]:
        """Return only user-authored comments from a ticket.

        Agent and system handoff messages are pipeline noise and should
        not be injected into any agent's initial context.
        """
        return [c for c in (ticket.get("comments") or []) if c.get("author") == "user"]

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

    def _load_tool_spill_threshold(self) -> int:
        """Load tool spill threshold (in bytes) from environment or config."""
        env_val = os.environ.get("TOOL_SPILL_THRESHOLD")
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                pass
        try:
            from orchestrator.config import _load_config_file

            cfg = _load_config_file()
            return int(
                cfg.get("tool_spill_threshold", self.DEFAULT_TOOL_SPILL_THRESHOLD)
            )
        except Exception:
            return self.DEFAULT_TOOL_SPILL_THRESHOLD

    def _spill_tool_output(
        self, tool_name: str, content: str, jq_filter: str | None = None
    ) -> str:
        """Spill large tool output to the ticket workspace if it exceeds threshold.

        If jq_filter is provided by the caller on a JSON output, the full output
        is saved to the workspace and the jq_filter is applied in-flight, returning
        the filtered result directly (or a preview if still large) in the same turn.
        """
        if not content:
            return content

        raw_bytes = content.encode("utf-8")
        if not jq_filter and len(raw_bytes) <= self._spill_threshold:
            return content

        # Exclude workspace inspection, skill/doc loading, user interaction, and submission tools
        exempt_tools = {
            # Workspace inspection
            "jq_query",
            "grep_file",
            "read_file_slice",
            "list_workspace_files",
            # Skill & documentation reading
            "read_skill",
            "read_skills",
            "read_harness_doc",
            "get_review_config",
            "get_execution_config",
            "get_example_runfile",
            "get_tool_params",
            # File reading with existing caller control
            "read_remote_file",
            # User interaction & checkpoints
            "request_clarification",
            "request_human_input",
            "present_runfile_for_approval",
        }
        if (
            tool_name in exempt_tools or tool_name.startswith("submit_")
        ) and not jq_filter:
            return content

        try:
            from providers.workspace.manager import WorkspaceManager

            manager = WorkspaceManager(ticket_id=self._current_ticket_id)
            self._tool_call_seq += 1

            # Determine extension
            is_json = False
            try:
                stripped = content.strip()
                if stripped.startswith(("{", "[")):
                    json.loads(stripped)
                    is_json = True
            except Exception:
                is_json = False

            ext = "json" if is_json else "txt"
            filename = f"{tool_name}_{self._tool_call_seq}.{ext}"
            file_ref, _ = manager.save_file(filename, content)

            # If caller passed a jq_filter and content is JSON, execute filter in-flight
            if jq_filter and is_json:
                q_res = manager.jq_query(
                    file_ref, jq_filter, limit=100, max_bytes=self._spill_threshold
                )
                if q_res.get("status") == "ok":
                    filtered_data = q_res.get("result")
                    filtered_json = json.dumps(
                        {
                            "status": "filtered",
                            "file_ref": file_ref,
                            "jq_filter": jq_filter,
                            "data": filtered_data,
                            "truncated": q_res.get("truncated", False),
                            "total_items": q_res.get("total_items"),
                            "full_size_bytes": len(raw_bytes),
                        },
                        indent=2,
                    )
                    if len(filtered_json.encode("utf-8")) <= self._spill_threshold:
                        logger.info(
                            f"[{self.agent_name}] In-flight jq_filter '{jq_filter}' on {tool_name} returned {len(filtered_json)} bytes (full: {file_ref})"
                        )
                        return filtered_json
                    else:
                        preview = manager.generate_preview(filename, filtered_json)
                        descriptor = {
                            "status": "spilled_to_workspace",
                            "tool_name": tool_name,
                            "file_ref": file_ref,
                            "jq_filter": jq_filter,
                            "format": ext,
                            "size_bytes": len(raw_bytes),
                            "preview": preview,
                            "message": (
                                f"Full output saved to '{file_ref}'. Filtered result ({len(filtered_json)} bytes) "
                                f"exceeds threshold. Refine jq_query or read slices."
                            ),
                        }
                        return json.dumps(descriptor, indent=2)
                else:
                    preview = manager.generate_preview(filename, content)
                    descriptor = {
                        "status": "filter_error",
                        "tool_name": tool_name,
                        "file_ref": file_ref,
                        "jq_filter": jq_filter,
                        "error": q_res.get("error"),
                        "preview": preview,
                    }
                    return json.dumps(descriptor, indent=2)

            preview = manager.generate_preview(filename, content)
            descriptor = {
                "status": "spilled_to_workspace",
                "tool_name": tool_name,
                "file_ref": file_ref,
                "format": ext,
                "size_bytes": len(raw_bytes),
                "preview": preview,
                "message": (
                    f"Tool output ({len(raw_bytes)} bytes) was saved to workspace as '{file_ref}'. "
                    f"Use jq_query (for JSON) or grep_file / read_file_slice (for text) to inspect relevant parts."
                ),
            }
            logger.info(
                f"[{self.agent_name}] Spilled {len(raw_bytes)} bytes from tool '{tool_name}' to {file_ref}"
            )
            return json.dumps(descriptor, indent=2)
        except Exception as e:
            logger.warning(
                f"[{self.agent_name}] Failed to spill tool output for {tool_name}: {e}"
            )
            return content

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

        call_input = dict(tool_call.input) if tool_call.input else {}
        jq_filter = None
        if tool_call.name != "jq_query":
            jq_filter = call_input.pop("jq_filter", None) or call_input.pop(
                "jq_query", None
            )
            if jq_filter is not None:
                jq_filter = str(jq_filter).strip() or None

        handler = self._tool_handlers.get(tool_call.name)
        if handler is not None:
            try:
                try:
                    result = await handler(**call_input)
                except TypeError:
                    result = await handler(**tool_call.input)

                if isinstance(result, str):
                    content = result
                else:
                    content = json.dumps(result, default=str)
                content = self._spill_tool_output(
                    tool_call.name, content, jq_filter=jq_filter
                )
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
                try:
                    content = await self._mcp.call_tool(tool_call.name, call_input)
                except Exception:
                    content = await self._mcp.call_tool(tool_call.name, tool_call.input)

                content = self._spill_tool_output(
                    tool_call.name, content, jq_filter=jq_filter
                )
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

    async def _handle_context_pause(self, ticket_id: str) -> None:
        """Handle context-window pause during agent execution.

        Delegates to _handle_budget_pause for the transition
        but posts a distinct comment so the user knows
        raising llm_budget will not help.
        """
        await self._add_comment(
            ticket_id,
            f"**Agent {self.agent_name} paused: context "
            f"window nearly full.**\n\n"
            f"The model's input context window is approaching "
            f"its limit. Raising the token budget will not "
            f"help — the conversation history is too large "
            f"for the model. Partial results may be available.",
        )
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=(f"{self.agent_name} context window full — pausing for guidance"),
        )

    async def _check_context(
        self,
        ticket_id: str,
        usage: dict[str, Any],
    ) -> str:
        """Check context usage against the model's window.

        Returns 'ok', 'warn', or 'pause'.
        """
        try:
            from orchestrator.config import _load_config_file
            from providers.context_guard import (
                ContextAction,
                check_context_usage,
                context_guard_from_config,
                context_guard_from_custom_fields,
            )
            from providers.cost import get_context_window

            config = _load_config_file()
            guard_cfg = context_guard_from_config(config)

            if not guard_cfg.get("enabled", True):
                return "ok"

            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            guard = context_guard_from_custom_fields(cf, guard_cfg)

            if not guard.get("enabled", True):
                return "ok"

            context_tokens = usage.get("context_tokens", 0)
            if context_tokens <= 0:
                return "ok"

            model = usage.get("model", "")
            config_default = guard.get("default_context_window", 0)
            if model:
                context_window = max(
                    get_context_window(model),
                    config_default,
                )
            else:
                context_window = config_default
            if context_window <= 0:
                return "ok"

            action, reason = check_context_usage(
                context_tokens,
                context_window,
                warn_pct=guard.get("warn_pct", 60.0),
                pause_pct=guard.get("pause_pct", 80.0),
            )

            if action == ContextAction.PAUSE:
                self._emit(
                    ticket_id,
                    "agent_error",
                    {
                        "reason": "context_window_full",
                        "detail": reason,
                    },
                )
                logger.warning(
                    f"[{self.agent_name}] Context window full on {ticket_id}: {reason}"
                )
                return "pause"

            if action == ContextAction.WARN:
                logger.info(
                    f"[{self.agent_name}] Context warning on {ticket_id}: {reason}"
                )
                return "warn"

        except ImportError:
            pass
        except Exception:
            logger.exception(f"[{self.agent_name}] Context check failed")

        return "ok"

    async def _check_budget(self, ticket_id: str) -> str:
        """Check per-ticket LLM budget and per-user quota.

        Returns 'ok', 'warn', or 'pause'. On 'pause', the agent
        transitions the ticket to awaiting_customer_guidance so
        the user can decide to increase the budget or abort.
        """
        result = "ok"

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
            if budget is not None:
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
                    result = "warn"

        except ImportError:
            pass
        except Exception:
            logger.exception(f"[{self.agent_name}] Budget check failed")

        # Per-user quota check (secondary in-loop enforcement
        # to bound overshoot between dispatch cycles).  Runs
        # independently of per-ticket budget so it is never
        # bypassed by a missing ticket-level budget config.
        try:
            if self._events is not None:
                ledger = getattr(self._events, "_usage_ledger", None)
                if ledger is not None:
                    ticket = await self._get_ticket(ticket_id)
                    created_by = ticket.get("created_by", "")
                    if created_by:
                        from orchestrator.config import _load_config_file
                        from providers.quota import (
                            check_user_quota,
                            resolve_quota_inputs,
                        )

                        config = _load_config_file()
                        user_store = getattr(
                            self,
                            "_user_store",
                            None,
                        )
                        if user_store is None:
                            user_store = getattr(
                                self._events,
                                "_user_store",
                                None,
                            )

                        if user_store is not None:
                            uq, gqs, is_svc = resolve_quota_inputs(
                                created_by,
                                user_store,
                                config,
                            )
                            quota_result = check_user_quota(
                                created_by,
                                uq,
                                gqs,
                                ledger,
                                is_service_account=is_svc,
                            )
                        else:
                            from providers.quota import quota_from_config

                            default_quota = quota_from_config(config)
                            quota_result = check_user_quota(
                                created_by,
                                default_quota,
                                None,
                                ledger,
                            )

                        if quota_result.exceeded and not quota_result.warn_only:
                            reason_text = "; ".join(quota_result.reasons)
                            logger.warning(
                                f"[{self.agent_name}] User quota exceeded "
                                f"for {created_by}: {reason_text}"
                            )
                            await self._add_comment(
                                ticket_id,
                                f"**User quota exceeded:** {reason_text}\n\n"
                                f"Agent pausing for quota reset.",
                            )
                            return "pause"
        except ImportError:
            pass
        except Exception:
            logger.exception(f"[{self.agent_name}] User quota check failed")

        return result

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
