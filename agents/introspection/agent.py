"""Introspection agent — continuous passive observer for running tickets.

This agent is out-of-band: it does NOT participate in the normal
agent execution chain and does NOT transition ticket state. It
runs as a companion task alongside the active agents, continuously
watching the event stream and updating its observations.

Phase 1: Passive observer (read-only, continuous).
Phase 2: Active monitor (soft-stop signals).
Phase 3: Corralling (guidance injection).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from providers.events import EventBus

from .server import (
    _detect_anomalies_from_events,
    _read_events,
    _truncate_event,
)

logger = logging.getLogger(__name__)

# Statuses where the ticket is done — stop watching.
_TERMINAL_STATUSES = frozenset({"closed"})

# How often to poll for new events (seconds).
_POLL_INTERVAL = 5.0

# How many events to fetch per poll cycle.
_POLL_BATCH_SIZE = 200


class IntrospectionAgent:
    """Continuous passive observer for a running ticket.

    Unlike other agents, the introspection agent:
    - Does NOT extend AgentBase (no LLM loop)
    - Does NOT transition ticket state
    - Does NOT participate in the dispatch loop
    - Runs as a background asyncio task alongside real agents
    - Polls the event stream and writes observations to
      custom_fields.introspection

    The agent runs until the ticket reaches a terminal status
    or is explicitly stopped via cancellation.
    """

    def __init__(
        self,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        self.store_url = state_store_url.rstrip("/")
        self._events = event_bus
        self._last_seq = 0
        self._all_events: list[dict[str, Any]] = []
        self._stop_requested = False
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

        Polls the JSONL event stream, detects anomalies, and writes
        a summary to custom_fields.introspection on each cycle that
        has new events.
        """
        logger.info(f"[introspection] Starting observation of {ticket_id}")

        if self._events:
            self._events.emit(
                ticket_id,
                "introspection-agent",
                "agent_started",
                {"mode": "continuous_observer"},
            )

        try:
            while not self._stop_requested:
                # Check if ticket has reached a terminal state.
                try:
                    ticket = await self._get_ticket(ticket_id)
                    status = ticket.get("status", "")
                    if status in _TERMINAL_STATUSES:
                        logger.info(
                            f"[introspection] Ticket {ticket_id}"
                            f" reached {status}, stopping"
                        )
                        break
                except Exception:
                    logger.debug(
                        f"[introspection] Failed to fetch"
                        f" ticket {ticket_id}, will retry"
                    )
                    await asyncio.sleep(_POLL_INTERVAL)
                    continue

                # Fetch new events since last poll.
                new_events = _read_events(
                    ticket_id,
                    since=self._last_seq,
                    limit=_POLL_BATCH_SIZE,
                )

                if new_events:
                    self._all_events.extend(new_events)
                    self._last_seq = new_events[-1].get(
                        "seq",
                        self._last_seq,
                    )

                    # Run anomaly detection on full history.
                    anomalies = _detect_anomalies_from_events(
                        self._all_events,
                    )

                    # Build observation summary.
                    observation = self._build_observation(
                        ticket,
                        new_events,
                        anomalies,
                    )

                    # Write to ticket custom_fields.
                    await self._update_observation(
                        ticket_id,
                        observation,
                    )

                await asyncio.sleep(_POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info(f"[introspection] Observation of {ticket_id} cancelled")
        except Exception:
            logger.exception(f"[introspection] Error observing {ticket_id}")
        finally:
            if self._events:
                self._events.emit(
                    ticket_id,
                    "introspection-agent",
                    "agent_finished",
                )
            logger.info(f"[introspection] Stopped observing {ticket_id}")

    def _build_observation(
        self,
        ticket: dict[str, Any],
        new_events: list[dict[str, Any]],
        anomalies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the observation dict for custom_fields.introspection."""
        # Compute per-agent event counts.
        agent_counts: dict[str, int] = {}
        tool_error_count = 0
        llm_call_count = 0
        for evt in self._all_events:
            agent = evt.get("agent", "")
            if agent:
                agent_counts[agent] = agent_counts.get(agent, 0) + 1
            if evt.get("event_type") == "llm_request":
                llm_call_count += 1
            if evt.get("event_type") == "tool_result" and evt.get("data", {}).get(
                "is_error"
            ):
                tool_error_count += 1

        # Build a concise status summary.
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

        # Build narrative from recent events (last batch).
        narrative_parts: list[str] = []
        for evt in new_events:
            trimmed = _truncate_event(evt)
            etype = trimmed.get("event_type", "")
            agent = trimmed.get("agent", "")
            data = trimmed.get("data", {})

            if etype == "agent_started":
                narrative_parts.append(f"{agent} started")
            elif etype == "agent_finished":
                narrative_parts.append(f"{agent} finished")
            elif etype == "transition":
                to = data.get("to", "?")
                narrative_parts.append(f"Transitioned to {to}")
            elif etype == "tool_called":
                tool = data.get("tool", "?")
                narrative_parts.append(f"{agent} called {tool}")
            elif etype == "tool_result" and data.get("is_error"):
                tool = data.get("tool", "?")
                narrative_parts.append(f"{agent}: {tool} returned error")
            elif etype == "agent_error":
                reason = data.get("reason", "unknown")
                narrative_parts.append(f"{agent} error: {reason}")

        narrative = "; ".join(narrative_parts) if narrative_parts else ""

        return {
            "narrative": narrative,
            "anomalies": anomalies,
            "status_summary": status_summary,
            "total_events": len(self._all_events),
            "agents_seen": agent_counts,
        }

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
