"""Introspection agent — continuous passive observer for running tickets.

This agent is out-of-band: it does NOT participate in the normal
agent execution chain and does NOT transition ticket state. It
runs as a companion task alongside the active agents, continuously
watching the event stream and updating its observations.

Architecture: hybrid deterministic + LLM.
- Deterministic loop: polls events every 5s, runs anomaly detection
  (code enforces invariants)
- LLM interpretation: called at key moments to produce narrative
  entries and the final summary (LLM decides interpretation)

When no LLM provider is configured, the agent falls back to
mechanical narrative entries (event-type one-liners).

Phase 1: Passive observer (read-only, continuous).
Phase 2: Active monitor (soft-stop signals).
Phase 3: Corralling (guidance injection).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from providers.events import EventBus
from providers.llm.base import LLMProvider
from state_store.models import TERMINAL_STATUSES as _MODEL_TERMINAL

from .server import (
    _detect_anomalies_from_events,
    _is_tool_failure,
    _read_events,
    _truncate_event,
)
from .skills import load_error_patterns, load_thresholds, load_tool_bypass_patterns

logger = logging.getLogger(__name__)

# Statuses where the ticket is done — stop watching.
# Derive from the state machine's canonical set so the
# introspection agent stays consistent if new terminals are added.
_TERMINAL_STATUSES = frozenset(s.value for s in _MODEL_TERMINAL)

# How often to poll for new events (seconds).
_POLL_INTERVAL = 5.0

# How many events to fetch per poll cycle.
_POLL_BATCH_SIZE = 200

# Maximum recent events to include in LLM context.
_LLM_CONTEXT_EVENTS = 30

# Skills directory for the observer prompt.
_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def _load_observer_prompt() -> str:
    """Load the observer system prompt from skills."""
    path = _SKILLS_DIR / "introspection" / "observer-prompt.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return (
        "You are observing a performance testing ticket. "
        "Provide concise analysis of pipeline operations."
    )


class IntrospectionAgent:
    """Continuous passive observer for a running ticket.

    Hybrid architecture:
    - Deterministic detection loop (always runs)
    - LLM narrative and final summary (when llm_provider is set)

    The agent does NOT extend AgentBase, does NOT transition
    ticket state, and does NOT participate in the dispatch loop.

    Note on state: unlike pipeline agents (which are stateless
    per AGENTS.md), the introspection agent intentionally
    maintains in-memory state (_all_events, _narrative_log)
    because it is a long-lived companion task, not a dispatch-
    loop agent.  If it crashes, the orchestrator does not
    restart it — the deterministic detection re-derives
    anomalies from the JSONL on the next ticket, so no
    durable state is lost.
    """

    async def __aenter__(self) -> IntrospectionAgent:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    def __init__(
        self,
        state_store_url: str,
        event_bus: EventBus | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.store_url = state_store_url.rstrip("/")
        self._events = event_bus
        self._llm = llm_provider
        self._last_seq = 0
        self._all_events: list[dict[str, Any]] = []
        self._narrative_log: list[str] = []
        self._prev_anomaly_count = 0
        self._prev_status = ""
        self._llm_call_count = 0
        self._stop_requested = False
        self._error_patterns = load_error_patterns()
        self._thresholds = load_thresholds()
        self._bypass_patterns = load_tool_bypass_patterns()
        headers: dict[str, str] = {}
        api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)

    def request_stop(self) -> None:
        """Request graceful shutdown of the observation loop."""
        self._stop_requested = True

    async def close(self) -> None:
        """Clean up HTTP client."""
        await self._client.aclose()

    async def run(self, ticket_id: str) -> None:
        """Continuously observe a ticket until it reaches a terminal state.

        Polls the JSONL event stream, runs deterministic anomaly
        detection, and calls the LLM for narrative interpretation
        at key moments.
        """
        logger.info(
            f"[introspection] Starting observation of {ticket_id}"
            f" (llm={'yes' if self._llm else 'no'})"
        )

        # Seed from existing ticket state so restarts don't
        # lose narrative history or fire spurious LLM triggers.
        await self._seed_from_ticket(ticket_id)

        if self._events:
            self._events.emit(
                ticket_id,
                "introspection-agent",
                "agent_started",
                {"mode": "continuous_observer"},
            )

        reached_terminal = False
        try:
            while not self._stop_requested:
                try:
                    ticket = await self._get_ticket(ticket_id)
                    status = ticket.get("status", "")
                    if status in _TERMINAL_STATUSES:
                        logger.info(
                            f"[introspection] Ticket {ticket_id}"
                            f" reached {status}, stopping"
                        )
                        reached_terminal = True
                        break
                except Exception:
                    logger.debug(
                        f"[introspection] Failed to fetch"
                        f" ticket {ticket_id}, will retry"
                    )
                    await asyncio.sleep(_POLL_INTERVAL)
                    continue

                # Fetch new events since last poll.
                # Run in a thread to avoid blocking the
                # event loop on file I/O.
                new_events = await asyncio.to_thread(
                    _read_events,
                    ticket_id,
                    self._last_seq,
                    _POLL_BATCH_SIZE,
                )

                if new_events:
                    self._all_events.extend(new_events)
                    self._last_seq = new_events[-1].get(
                        "seq",
                        self._last_seq,
                    )

                    # Deterministic anomaly detection.
                    anomalies = _detect_anomalies_from_events(
                        self._all_events,
                        error_patterns=self._error_patterns,
                        thresholds=self._thresholds,
                        bypass_patterns=self._bypass_patterns,
                    )

                    # Check if LLM narrative is warranted.
                    llm_narrative = await self._maybe_narrate(
                        ticket_id,
                        ticket,
                        new_events,
                        anomalies,
                    )

                    # Build observation with narrative.
                    observation = self._build_observation(
                        ticket,
                        new_events,
                        anomalies,
                        llm_narrative,
                    )

                    await self._update_observation(
                        ticket_id,
                        observation,
                    )

                await asyncio.sleep(_POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info(f"[introspection] Observation of {ticket_id} cancelled")
            # Check if ticket reached terminal while we were
            # cancelled (e.g., stop_agent during close).
            if not reached_terminal:
                try:
                    t = await self._get_ticket(ticket_id)
                    if t.get("status", "") in _TERMINAL_STATUSES:
                        reached_terminal = True
                except Exception:
                    pass
        except Exception:
            logger.exception(f"[introspection] Error observing {ticket_id}")
        finally:
            # Final flush and LLM summary on terminal status.
            if reached_terminal:
                await asyncio.sleep(_POLL_INTERVAL)
                try:
                    ticket = await self._get_ticket(ticket_id)
                except Exception:
                    ticket = {"status": "closed"}

                # Catch trailing events.
                trailing = await asyncio.to_thread(
                    _read_events,
                    ticket_id,
                    self._last_seq,
                    _POLL_BATCH_SIZE,
                )
                if trailing:
                    self._all_events.extend(trailing)

                # Write final summary.
                await self._write_final_summary(ticket_id, ticket)

            if self._events:
                self._events.emit(
                    ticket_id,
                    "introspection-agent",
                    "agent_finished",
                )
            logger.info(f"[introspection] Stopped observing {ticket_id}")

    # --- LLM narrative ---

    async def _maybe_narrate(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
        new_events: list[dict[str, Any]],
        anomalies: list[dict[str, Any]],
    ) -> str | None:
        """Call the LLM for narrative when something significant happens.

        Returns narrative text, or None if no LLM call was made.

        Triggers:
        - New anomaly detected (count increased)
        - Agent transition (status changed)
        """
        if not self._llm:
            return None

        # Check triggers.
        new_anomaly = len(anomalies) > self._prev_anomaly_count
        status = ticket.get("status", "")
        status_changed = status != self._prev_status and self._prev_status

        if not new_anomaly and not status_changed:
            return None

        narrative = await self._llm_narrate(
            ticket_id,
            ticket,
            new_events,
            anomalies,
            trigger="anomaly" if new_anomaly else "transition",
        )

        # Only update state trackers after successful narration
        # so transient LLM failures don't permanently lose the
        # trigger.
        if narrative is not None:
            self._prev_anomaly_count = len(anomalies)
            self._prev_status = status

        return narrative

    def _record_usage(
        self,
        ticket_id: str,
        response: Any,
    ) -> None:
        """Record LLM token usage for cost accounting."""
        if not response.usage or not self._events:
            return
        usage = response.usage
        # Normalize to dict — some providers may return an
        # object with attributes instead of a plain dict.
        if not isinstance(usage, dict):
            usage = {
                k: getattr(usage, k, 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "model",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            }
        self._events.record_llm_usage(
            ticket_id=ticket_id,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            duration_ms=0,
            model=usage.get("model", ""),
            agent_name="introspection-agent",
            cache_read_input_tokens=usage.get(
                "cache_read_input_tokens",
                0,
            ),
            cache_creation_input_tokens=usage.get(
                "cache_creation_input_tokens",
                0,
            ),
        )
        self._events.emit(
            ticket_id,
            "introspection-agent",
            "llm_usage",
            usage,
        )

    async def _llm_narrate(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
        new_events: list[dict[str, Any]],
        anomalies: list[dict[str, Any]],
        trigger: str = "",
    ) -> str | None:
        """Make an LLM call for narrative interpretation."""
        assert self._llm is not None
        self._llm_call_count += 1

        system_prompt = _load_observer_prompt()

        # Build a compact context for the LLM: recent events
        # (truncated for token efficiency) + current anomalies.
        recent = [_truncate_event(e) for e in self._all_events[-_LLM_CONTEXT_EVENTS:]]

        user_msg = (
            f"## Current State\n"
            f"Ticket: {ticket.get('id', '?')}\n"
            f"Status: {ticket.get('status', '?')}\n"
            f"Total events: {len(self._all_events)}\n"
            f"Trigger: {trigger}\n\n"
            f"## Recent Events\n"
            f"```json\n{json.dumps(recent, indent=1, default=str)}\n```\n\n"
        )

        if anomalies:
            user_msg += (
                f"## Detected Anomalies\n"
                f"```json\n"
                f"{json.dumps(anomalies, indent=1, default=str)}"
                f"\n```\n\n"
            )

        user_msg += (
            "Provide a brief (1-3 sentence) narrative observation "
            "about what is happening right now in the pipeline. "
            "Focus on patterns, root causes, and operational "
            "concerns — not individual events."
        )

        try:
            response = await self._llm.complete(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
            )
            self._record_usage(ticket_id, response)
            return response.text
        except Exception:
            logger.debug(
                "[introspection] LLM narrative call failed",
                exc_info=True,
            )
            return None

    # --- Final summary ---

    async def _write_final_summary(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
    ) -> None:
        """Write the final introspection summary on ticket close.

        Uses the LLM (if available) to produce a reasoned summary
        with verdict, observations, and recommendations. Falls back
        to deterministic stats if no LLM is configured.
        """
        anomalies = _detect_anomalies_from_events(
            self._all_events,
            error_patterns=self._error_patterns,
            thresholds=self._thresholds,
            bypass_patterns=self._bypass_patterns,
        )
        stats = self._compute_stats()

        if self._llm:
            summary = await self._llm_final_summary(
                ticket_id,
                ticket,
                anomalies,
                stats,
            )
        else:
            summary = self._deterministic_final_summary(
                anomalies,
                stats,
            )

        try:
            await self._client.patch(
                f"{self.store_url}/api/v1/tickets/{ticket_id}/fields",
                json={
                    "fields": {"introspection_summary": summary},
                },
            )
            logger.info(f"[introspection] Final summary written for {ticket_id}")
        except Exception:
            logger.warning(
                f"[introspection] Failed to write final summary for {ticket_id}",
                exc_info=True,
            )

    async def _llm_final_summary(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
        anomalies: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce the final summary using the LLM."""
        assert self._llm is not None

        system_prompt = _load_observer_prompt()

        # Include full anomaly list and stats. For events,
        # include a representative sample — not the full
        # history, which could be thousands of events.
        sample_events = [_truncate_event(e) for e in self._all_events[-50:]]

        user_msg = (
            f"## Final Summary Request\n\n"
            f"The ticket has closed. Produce a final introspection "
            f"summary of how the pipeline operated.\n\n"
            f"**Ticket:** {ticket.get('id', '?')}\n"
            f"**Summary:** {ticket.get('summary', '?')}\n\n"
            f"## Aggregate Stats\n"
            f"```json\n{json.dumps(stats, indent=1, default=str)}"
            f"\n```\n\n"
        )

        if anomalies:
            user_msg += (
                f"## Anomalies Detected\n"
                f"```json\n"
                f"{json.dumps(anomalies, indent=1, default=str)}"
                f"\n```\n\n"
            )
        else:
            user_msg += "## Anomalies Detected\nNone.\n\n"

        user_msg += (
            f"## Recent Events (last 50 of "
            f"{len(self._all_events)})\n"
            f"```json\n"
            f"{json.dumps(sample_events, indent=1, default=str)}"
            f"\n```\n\n"
            f"Respond with a JSON object containing:\n"
            f"- verdict: 'clean', 'minor_issues', or "
            f"'needs_attention'\n"
            f"- observations: list of key observations (strings)\n"
            f"- recommendations: list of objects with 'area' "
            f"(infrastructure/agent_logic/efficiency/convergence) "
            f"and 'suggestion' (specific, actionable)\n"
        )

        try:
            response = await self._llm.complete(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
            )
            self._record_usage(ticket_id, response)
            # Parse the LLM's JSON response.
            parsed = self._parse_summary_response(response.text)
            # Merge with deterministic stats — LLM provides
            # the reasoning, code provides the numbers.
            parsed["stats"] = stats
            parsed["anomalies"] = anomalies
            return parsed
        except Exception:
            logger.warning(
                "[introspection] LLM final summary failed,"
                " falling back to deterministic",
                exc_info=True,
            )
            return self._deterministic_final_summary(
                anomalies,
                stats,
            )

    @staticmethod
    def _parse_summary_response(text: str | None) -> dict[str, Any]:
        """Parse the LLM's final summary response."""
        if not text:
            return {}
        text = text.strip()
        # Try direct JSON parse.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Extract from code fence.
        fence = re.search(
            r"```(?:json)?\s*\n(.*?)\n```",
            text,
            re.DOTALL,
        )
        if fence:
            try:
                return json.loads(fence.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Fall back: treat the whole text as a narrative.
        return {
            "verdict": "unknown",
            "observations": [text[:500]],
            "recommendations": [],
        }

    def _deterministic_final_summary(
        self,
        anomalies: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce a stats-only summary when no LLM is available."""
        high = sum(1 for a in anomalies if a.get("severity") == "high")
        if high == 0 and stats.get("total_tool_errors", 0) == 0:
            verdict = "clean"
        elif high == 0:
            verdict = "minor_issues"
        else:
            verdict = "needs_attention"

        return {
            "verdict": verdict,
            "observations": [],
            "recommendations": [],
            "stats": stats,
            "anomalies": anomalies,
        }

    # --- Stats computation ---

    def _compute_stats(self) -> dict[str, Any]:
        """Compute aggregate stats from accumulated events."""
        agent_stats: dict[str, dict[str, int]] = {}
        first_ts = ""
        last_ts = ""

        for evt in self._all_events:
            agent = evt.get("agent", "")
            if not agent:
                continue
            if agent not in agent_stats:
                agent_stats[agent] = {
                    "events": 0,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "tool_errors": 0,
                }
            s = agent_stats[agent]
            s["events"] += 1
            etype = evt.get("event_type", "")
            if etype == "llm_request":
                s["llm_calls"] += 1
            elif etype == "tool_called":
                s["tool_calls"] += 1
            elif etype == "tool_result" and _is_tool_failure(evt):
                s["tool_errors"] += 1

            ts = evt.get("timestamp", "")
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

        total_llm = sum(s.get("llm_calls", 0) for s in agent_stats.values())
        total_errors = sum(s.get("tool_errors", 0) for s in agent_stats.values())

        return {
            "total_events": len(self._all_events),
            "first_event": first_ts,
            "last_event": last_ts,
            "total_llm_calls": total_llm,
            "total_tool_errors": total_errors,
            "introspection_llm_calls": self._llm_call_count,
            "per_agent": agent_stats,
        }

    # --- Observation building ---

    def _build_observation(
        self,
        ticket: dict[str, Any],
        new_events: list[dict[str, Any]],
        anomalies: list[dict[str, Any]],
        llm_narrative: str | None = None,
    ) -> dict[str, Any]:
        """Build the observation dict for custom_fields.introspection."""
        agent_counts: dict[str, int] = {}
        tool_error_count = 0
        llm_call_count = 0
        for evt in self._all_events:
            agent = evt.get("agent", "")
            if agent:
                agent_counts[agent] = agent_counts.get(agent, 0) + 1
            if evt.get("event_type") == "llm_request":
                llm_call_count += 1
            if _is_tool_failure(evt):
                tool_error_count += 1

        status = ticket.get("status", "unknown")
        status_summary = (
            f"Ticket is in '{status}' with"
            f" {len(self._all_events)} events observed"
            f" ({llm_call_count} LLM calls,"
            f" {tool_error_count} tool errors)."
        )
        if anomalies:
            high = sum(1 for a in anomalies if a.get("severity") == "high")
            med = sum(1 for a in anomalies if a.get("severity") == "medium")
            status_summary += (
                f" {len(anomalies)} anomal"
                f"{'y' if len(anomalies) == 1 else 'ies'}"
                f" detected"
                f" ({high} high, {med} medium)."
            )

        # LLM narrative entries are prefixed to distinguish
        # them from mechanical entries in the UI.
        if llm_narrative:
            self._narrative_log.append(f"[observation] {llm_narrative}")

        # Mechanical entries as fallback / background log.
        for evt in new_events:
            trimmed = _truncate_event(evt)
            etype = trimmed.get("event_type", "")
            agent = trimmed.get("agent", "")
            data = trimmed.get("data", {})
            entry = None

            if etype == "agent_started":
                entry = f"{agent} started"
            elif etype == "agent_finished":
                entry = f"{agent} finished"
            elif etype == "transition":
                to = data.get("to", "?")
                entry = f"Transitioned to {to}"
            elif etype == "tool_called":
                tool = data.get("tool", "?")
                entry = f"{agent} called {tool}"
            elif etype == "tool_result" and data.get("is_error"):
                tool = data.get("tool", "?")
                entry = f"{agent}: {tool} returned error"
            elif etype == "agent_error":
                reason = data.get("reason", "unknown")
                entry = f"{agent} error: {reason}"

            if entry:
                self._narrative_log.append(entry)

        max_entries = 200
        if len(self._narrative_log) > max_entries:
            self._narrative_log = self._narrative_log[-max_entries:]

        return {
            "narrative": list(self._narrative_log),
            "anomalies": anomalies,
            "status_summary": status_summary,
            "total_events": len(self._all_events),
            "agents_seen": agent_counts,
        }

    # --- Startup seeding ---

    async def _seed_from_ticket(
        self,
        ticket_id: str,
    ) -> None:
        """Seed in-memory state from existing ticket data.

        On restart (e.g., after mark_done stops the previous
        instance), the new agent instance starts with empty
        state. This method restores:
        - _narrative_log from the ticket's existing narrative
          so history is not lost
        - _prev_status so status-change triggers don't fire
          spuriously on the first poll
        - _prev_anomaly_count so existing anomalies don't
          re-trigger LLM narrative calls
        """
        try:
            ticket = await self._get_ticket(ticket_id)
        except Exception:
            return

        status = ticket.get("status", "")
        if status:
            self._prev_status = status

        cf = ticket.get("custom_fields", {})
        intro = cf.get("introspection", {})

        # Restore narrative history.
        existing = intro.get("narrative", [])
        if isinstance(existing, list) and existing:
            self._narrative_log = list(existing)
            logger.debug(
                f"[introspection] Seeded {len(existing)} narrative entries from ticket"
            )

        # Seed anomaly count from existing observations.
        anomalies = intro.get("anomalies", [])
        if isinstance(anomalies, list):
            self._prev_anomaly_count = len(anomalies)

    # --- HTTP helpers ---

    async def _update_observation(
        self,
        ticket_id: str,
        observation: dict[str, Any],
    ) -> None:
        """Write observation to ticket custom_fields."""
        try:
            await self._client.patch(
                f"{self.store_url}/api/v1/tickets/{ticket_id}/fields",
                json={"fields": {"introspection": observation}},
            )
        except Exception:
            logger.debug(
                f"[introspection] Failed to update observation for {ticket_id}"
            )

    async def _get_ticket(
        self,
        ticket_id: str,
    ) -> dict[str, Any]:
        """Fetch current ticket state."""
        r = await self._client.get(
            f"{self.store_url}/api/v1/tickets/{ticket_id}",
        )
        r.raise_for_status()
        return r.json()
