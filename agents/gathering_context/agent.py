"""Gathering Context agent: dedup gate against Investigation Records.

Checks whether an incoming anomaly matches an open Investigation
Record. If matched, the agent appends a build_history entry and
skips the full investigation. If no match, proceeds to planning.

Only runs for investigation-mode tickets (those routed to
gathering_context by triage). Ad-hoc tickets never enter this
status and are unaffected.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .prompts import (
    EXTERNAL_PERF_DATA_GUIDANCE,
    EXTERNAL_PERF_TOOL_NAMES,
    GATHERING_CONTEXT_SYSTEM_PROMPT,
    WEBHOOK_GROUNDING_GUIDANCE,
)

logger = logging.getLogger(__name__)


class GatheringContextAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(
            agent_name="gathering-context-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
        )

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        prompt = GATHERING_CONTEXT_SYSTEM_PROMPT
        tool_names = {t.name for t in self.tools} if self.tools else set()
        if tool_names & EXTERNAL_PERF_TOOL_NAMES:
            prompt += EXTERNAL_PERF_DATA_GUIDANCE
        # Webhook tickets need guidance on resolving directives
        # from the alert data.
        cf = ticket.get("custom_fields", {})
        if cf.get("trigger_source") or cf.get("anomaly_context", {}).get("source"):
            prompt += WEBHOOK_GROUNDING_GUIDANCE
        return prompt

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        anomaly = cf.get("anomaly_context", {})
        hypothesis = cf.get("hypothesis", "")

        content = (
            f"## Investigation Ticket\n\n**Summary:** {ticket.get('summary', '')}\n\n"
        )

        if anomaly:
            content += "**Anomaly Context:**\n"
            # Present all anomaly fields — webhook tickets have
            # different fields than manually submitted ones.
            for key, val in anomaly.items():
                if val is not None and val != "":
                    content += f"- {key}: {val}\n"
            content += "\n"
            # Flag webhook-triggered tickets so the LLM knows
            # it must resolve directives from the alert data.
            if anomaly.get("source"):
                content += (
                    f"**This is a webhook-triggered ticket** "
                    f"(source: {anomaly['source']}). "
                    f"Directives (board_selector, image_version, "
                    f"harness) may be missing and must be resolved "
                    f"from the alert data.\n\n"
                )
        else:
            content += (
                "**No anomaly context found.** This ticket has no "
                "structured anomaly data to match against Investigation "
                "Records. Submit a NO_MATCH result to proceed.\n\n"
            )

        if hypothesis:
            content += f"**Hypothesis:** {hypothesis}\n\n"

        content += (
            "Check open Investigation Records for this subsystem "
            "and evaluate whether this anomaly has already been "
            "investigated.\n"
        )

        return [{"role": "user", "content": content}]

    async def run(self, ticket_id: str) -> None:
        gc_server = str(Path(__file__).with_name("server.py"))
        ir_server = str(Path(__file__).parent.parent / "investigation" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(gc_server, name="gathering-context")
        await mcp.connect(ir_server, name="investigation-records")

        # Connect any configured external MCP servers
        # (e.g., domain knowledge, historical data).
        from agents.mcp_client import connect_external_servers

        connected_ext, ext_tools = await connect_external_servers(
            mcp, "gathering_context"
        )

        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        if ext_tools is not None:
            mcp_tools = [
                t
                for t in mcp_tools
                if mcp._tool_routing.get(t.name) not in connected_ext
                or t.name in ext_tools
            ]
        self.tools = mcp_tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        result = self._get_submit_result(response)
        if result is None:
            result = self._parse_json_response(response.text)

        decision = result.get("decision", "NO_MATCH")
        matched_id = result.get("matched_investigation_id", "")
        confidence = result.get("match_confidence", 0.0)
        rationale = result.get("match_rationale", "")
        notes = result.get("notes", "")

        # Persist the decision on the ticket
        fields: dict[str, Any] = {
            "dedup_result": {
                "decision": decision,
                "matched_investigation_id": matched_id,
                "match_confidence": confidence,
                "match_rationale": rationale,
                "notes": notes,
            },
        }

        # Write resolved directives from webhook grounding
        # so downstream agents know what hardware/image/harness
        # to use.
        directives = result.get("directives")
        if directives and isinstance(directives, dict):
            ticket = await self._get_ticket(ticket_id)
            existing = ticket.get(
                "custom_fields",
                {},
            ).get("directives", {})
            # Merge — don't overwrite directives the user set
            for k, v in directives.items():
                if v and k not in existing:
                    existing[k] = v
            fields["directives"] = existing

        await self._update_fields(ticket_id, fields)

        if decision == "MATCH_FOUND" and matched_id:
            summary = (
                f"**Dedup Match Found**\n\n"
                f"- **Matched Record:** {matched_id}\n"
                f"- **Confidence:** {confidence}\n"
                f"- **Rationale:** {rationale}\n\n"
                f"Skipping full investigation — this anomaly "
                f"matches an open Investigation Record."
            )
            await self._add_comment(ticket_id, summary)
            # Skip investigation, go to retrospective
            # for transcript analysis before closing
            await self._transition_ticket(
                ticket_id,
                "retrospective_pending",
                comment=(f"Dedup match: {matched_id}. Skipping investigation."),
            )
        else:
            summary = (
                "**No Dedup Match**\n\n"
                "No open Investigation Records match this anomaly. "
                "Proceeding to investigation planning."
            )
            if notes:
                summary += f"\n\n**Notes:** {notes}"
            await self._add_comment(ticket_id, summary)
            await self._transition_ticket(
                ticket_id,
                "planning_investigation",
                comment="No dedup match, proceeding to investigation",
            )
