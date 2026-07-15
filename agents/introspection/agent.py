"""Introspection agent — real-time passive observer for running tickets.

This agent is out-of-band: it does NOT participate in the normal
agent execution chain and does NOT transition ticket state. It
watches the event stream and provides analysis to the user.

Phase 1: Passive observer (read-only).
Phase 2: Active monitor (soft-stop signals).
Phase 3: Corralling (guidance injection).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import get_introspection_tools
from .prompts import INTROSPECTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Tools served via MCP (everything except submit_observation)
_MCP_TOOL_NAMES = frozenset(
    t.name for t in get_introspection_tools() if t.name != "submit_observation"
)


class IntrospectionAgent(AgentBase):
    """Passive observer agent for monitoring ticket execution.

    Unlike other agents, the introspection agent:
    - Does NOT transition ticket state
    - Does NOT participate in the dispatch loop
    - Is invoked on-demand (not by the orchestrator)
    - Reads the event stream but does not write to it
      (except its own observation events)
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        # Only keep submit_observation as a local tool;
        # the read-only tools are served via MCP.
        local_tools = [
            t for t in get_introspection_tools() if t.name not in _MCP_TOOL_NAMES
        ]

        super().__init__(
            agent_name="introspection-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers={},
            event_bus=event_bus,
            # Introspection is lightweight — fewer iterations
            # needed since it's summarizing, not executing.
            max_iterations=10,
        )

    async def run(self, ticket_id: str) -> None:
        """Run introspection on a ticket.

        Connects to the MCP server for read-only event access,
        then runs the standard LLM loop with observation tools.
        """
        introspection_server = str(Path(__file__).with_name("server.py"))

        mcp = AgentMCPClient()
        await mcp.connect(
            introspection_server,
            name="introspection",
            env={
                "TICKET_ID": ticket_id,
                "STATE_STORE_URL": self.store_url,
            },
        )
        self._mcp = mcp
        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return INTROSPECTION_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Introspection Request\n\n"
            f"**Ticket ID:** {ticket['id']}\n"
            f"**Summary:** {ticket['summary']}\n"
            f"**Current Status:** {ticket['status']}\n\n"
            f"Observe the event stream for ticket {ticket['id']}. "
            f"Use get_ticket_events to read the event log, "
            f"detect_anomalies to check for problems, and "
            f"get_token_usage to monitor resource consumption. "
            f"Then submit your observation with submit_observation."
        )

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        """Handle the introspection result.

        Unlike other agents, introspection does NOT transition
        the ticket. It stores its observation in custom_fields
        and adds a comment for visibility.
        """
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)

        narrative = result.get("narrative", "No narrative produced.")
        anomalies = result.get("anomalies", [])
        status_summary = result.get(
            "status_summary",
            "No status summary.",
        )

        # Store observation on the ticket without affecting
        # its state machine position.
        await self._update_fields(
            ticket_id,
            {
                "introspection": {
                    "narrative": narrative,
                    "anomalies": anomalies,
                    "status_summary": status_summary,
                },
            },
        )

        # Post a human-readable comment
        if anomalies:
            anomaly_lines = []
            for a in anomalies:
                severity = a.get("severity", "?")
                desc = a.get("description", "")
                anomaly_lines.append(f"  - [{severity}] {desc}")
            anomaly_text = "\n".join(anomaly_lines)
            comment = (
                f"**Introspection Report**\n\n"
                f"{status_summary}\n\n"
                f"**Anomalies ({len(anomalies)}):**\n"
                f"{anomaly_text}"
            )
        else:
            comment = (
                f"**Introspection Report**\n\n"
                f"{status_summary}\n\n"
                f"No anomalies detected."
            )

        await self._add_comment(ticket_id, comment)
        # NOTE: No state transition — introspection is out-of-band.
