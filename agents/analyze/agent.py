"""Analysis agent: investigate using existing data before provisioning hardware.

Queries external MCP tools (Domain MCP), prior ticket results, and
investigation records to answer performance questions. If the
available data is sufficient, submits findings and advances to
review. If inconclusive, explains what's missing and advances to
hardware provisioning for new measurements.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .prompts import ANALYZE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class AnalyzeAgent(AgentBase):
    """Analyze existing data to investigate performance questions."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(
            agent_name="analyze-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
        )

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return ANALYZE_SYSTEM_PROMPT

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        return [{"role": "user", "content": self._build_context(ticket, cf)}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        """Process the analysis result and transition the ticket."""
        analysis = self._get_submit_result(response)
        if analysis is None:
            analysis = self._parse_json_response(response.text)

        if analysis is None:
            analysis = {
                "conclusive": False,
                "finding": ("Analysis agent completed without submitting findings."),
            }

        await self._update_fields(
            ticket_id,
            {"analysis_result": analysis},
        )

        conclusive = analysis.get("conclusive", False)
        finding = analysis.get("finding", "")

        if conclusive:
            summary = f"**Analysis Complete** (conclusive)\n\n**Finding:** {finding}\n"
            root_cause = analysis.get("root_cause", "")
            if root_cause:
                summary += f"\n**Root Cause:** {root_cause}\n"
            await self._add_comment(ticket_id, summary)

            if not await self._plan_controls_next_transition(
                ticket_id,
            ):
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_review",
                    comment="Analysis conclusive, advancing to review",
                )
        else:
            benchmark_info = analysis.get("benchmark_needed", {})
            reason = benchmark_info.get(
                "reason",
                "Insufficient data for conclusive analysis",
            )
            summary = (
                "**Analysis Complete** (inconclusive)\n\n"
                f"**Finding:** {finding}\n"
                f"\n**Benchmark Needed:** {reason}\n"
            )
            suggested = benchmark_info.get("suggested_params")
            if suggested:
                summary += (
                    f"\n**Suggested Parameters:** {json.dumps(suggested, indent=2)}\n"
                )
            await self._add_comment(ticket_id, summary)

            if not await self._plan_controls_next_transition(
                ticket_id,
            ):
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_hardware",
                    comment=("Analysis inconclusive, need benchmark data"),
                )

    async def run(self, ticket_id: str) -> None:
        """Run the analysis agent with MCP tool connections."""
        # Connect the analysis agent's own MCP server
        server_path = str(Path(__file__).with_name("server.py"))
        mcp = AgentMCPClient()
        await mcp.connect(server_path, name="analyze")

        # Connect external MCP servers (Domain MCP, etc.)
        from agents.mcp_client import connect_external_servers

        connected_ext, ext_tools = await connect_external_servers(mcp, "analyze")

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

    def _build_context(
        self,
        ticket: dict[str, Any],
        cf: dict[str, Any],
    ) -> str:
        """Build the initial context message for the LLM."""
        parts = [
            f"## Investigation: {ticket.get('summary', '')}",
            "",
            f"**Description:** {ticket.get('description', '')}",
            "",
        ]

        hypothesis = cf.get("hypothesis")
        if hypothesis:
            parts.append(f"**Hypothesis:** {hypothesis}")
            parts.append("")

        directives = cf.get("directives", {})
        if directives:
            parts.append(f"**Directives:** {json.dumps(directives, indent=2)}")
            parts.append("")

        anomaly = cf.get("anomaly_context")
        if anomaly:
            parts.append(f"**Anomaly Context:** {json.dumps(anomaly, indent=2)}")
            parts.append("")

        run_meta = cf.get("run_metadata")
        if run_meta:
            parts.append(f"**Run Metadata:** {json.dumps(run_meta, indent=2)}")
            parts.append("")

        ref_tickets = cf.get("reference_tickets", [])
        if ref_tickets:
            parts.append(f"**Reference Tickets:** {', '.join(ref_tickets)}")
            parts.append(
                "Use get_ticket_results to retrieve data from "
                "these tickets for comparison."
            )
            parts.append("")

        # Extract run IDs mentioned in the description
        # or anomaly context for direct querying
        import re

        cited_run_ids: list[str] = []
        description = ticket.get("description", "")
        # Match patterns like "Run 234571" or "run_id: 283494"
        for match in re.findall(
            r"(?:Run|run_id|run)[:\s]+?(\d{5,})",
            description,
        ):
            if match not in cited_run_ids:
                cited_run_ids.append(match)
        # Also check anomaly_context.run_id
        if anomaly and anomaly.get("run_id"):
            rid = str(anomaly["run_id"])
            if rid not in cited_run_ids:
                cited_run_ids.append(rid)

        if cited_run_ids:
            parts.append(f"**Cited Run IDs:** {', '.join(cited_run_ids)}")
            parts.append(
                "Use `get_run_info` to query each run ID "
                "for metadata (target, OS, build, timing). "
                "These are specific data points referenced "
                "in the investigation."
            )
            parts.append("")

        harness = directives.get("harness", "")
        if harness:
            parts.append(
                f"**Harness:** {harness} — start by reading "
                f"the investigation methodology skill: "
                f"`list_skill_docs('{harness}')` then "
                f"`read_skill('{harness}', "
                f"'investigation-methodology.md')`"
            )
            parts.append("")

        parts.append(
            "Analyze the available data using the tools provided. "
            "Query external data sources, prior tickets, and "
            "investigation records. Then submit your findings "
            "via submit_analysis_result."
        )

        return "\n".join(parts)
