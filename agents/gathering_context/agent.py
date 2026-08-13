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
            # Present all anomaly fields — webhook tickets
            # may have different fields than manual ones.
            for key, val in anomaly.items():
                if val is not None and val != "":
                    content += f"- {key}: {val}\n"
            content += "\n"
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

    async def _deterministic_dedup(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
    ) -> bool:
        """Check for dedup match using canonical dedup fields.

        The ticket's ``dedup_key`` (set at ingestion by webhook
        enrichment, ticket creation, or any upstream process)
        contains normalized ``metric`` and ``platform`` values.
        These are compared against open investigation records
        using exact matching.

        This is source-agnostic — any ticket creator that sets
        ``dedup_key.metric`` and ``dedup_key.platform`` gets
        deterministic dedup. Source-specific field extraction
        belongs in the webhook translator or enrichment layer,
        not here.

        Returns True if a match was found and the ticket was
        transitioned.
        """
        cf = ticket.get("custom_fields", {})
        dedup_key = cf.get("dedup_key", {})

        dedup_metric = dedup_key.get("metric", "")
        dedup_platform = dedup_key.get("platform", "")

        if not dedup_metric or not dedup_platform:
            return False

        from providers.investigation.registry import (
            create_record_provider,
        )

        try:
            provider = create_record_provider()
        except Exception:
            logger.debug(
                "[gathering-context] Investigation records "
                "provider not available for deterministic "
                "dedup"
            )
            return False

        try:
            records = await provider.query(
                state="open",
                metric=dedup_metric,
                platform=dedup_platform,
                limit=10,
            )
        except Exception:
            logger.warning(
                "[gathering-context] Failed to query "
                "investigation records for deterministic "
                "dedup — falling back to LLM"
            )
            return False

        if not records:
            return False

        matched = records[0]
        matched_id = matched.investigation_id
        logger.info(
            f"[gathering-context] Deterministic dedup "
            f"match: {matched_id} "
            f"(metric={dedup_metric}, "
            f"platform={dedup_platform})"
        )

        # Record that the anomaly persists in this build
        run_meta = cf.get("run_metadata", {})
        anomaly = cf.get("anomaly_context", {})
        build_id = run_meta.get(
            "build",
            str(anomaly.get("run_id", "unknown")),
        )
        from providers.investigation.models import (
            BuildHistoryEntry,
        )

        try:
            entry = BuildHistoryEntry(
                build_id=str(build_id),
                action="SKIP_MATCHED",
                comment=(f"Deterministic dedup from ticket {ticket_id}"),
            )
            await provider.append_build_history(
                matched_id,
                entry,
            )
            logger.info(
                f"[gathering-context] Appended build "
                f"history to {matched_id}: {build_id}"
            )
        except Exception as e:
            logger.warning(
                f"[gathering-context] Failed to append "
                f"build history to {matched_id}: {e}"
            )

        await self._update_fields(
            ticket_id,
            {
                "dedup_result": {
                    "decision": "MATCH_FOUND",
                    "matched_investigation_id": matched_id,
                    "match_confidence": 1.0,
                    "match_rationale": (
                        "Deterministic match on "
                        f"metric='{dedup_metric}' "
                        f"platform='{dedup_platform}'"
                    ),
                    "match_method": "deterministic",
                },
            },
        )

        summary = (
            "**Dedup Match Found** "
            "(deterministic)\n\n"
            f"- **Matched Record:** {matched_id}\n"
            f"- **Metric:** {dedup_metric}\n"
            f"- **Platform:** {dedup_platform}\n\n"
            "Skipping investigation — this anomaly "
            "matches an open Investigation Record."
        )
        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "retrospective_pending",
            comment=(
                f"Deterministic dedup match: {matched_id}. Skipping investigation."
            ),
        )
        return True

    async def run(self, ticket_id: str) -> None:
        # Try deterministic dedup first — no LLM needed
        ticket = await self._get_ticket(ticket_id)
        if await self._deterministic_dedup(
            ticket_id,
            ticket,
        ):
            return

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
                "Proceeding to data analysis."
            )
            if notes:
                summary += f"\n\n**Notes:** {notes}"
            await self._add_comment(ticket_id, summary)
            await self._transition_ticket(
                ticket_id,
                "analyzing",
                comment="No dedup match, analyzing existing data",
            )
