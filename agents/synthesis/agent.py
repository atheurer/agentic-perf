"""Synthesis agent: produce Investigation Records on convergence.

When the evaluate agent fires a convergence gate, the synthesis
agent packages the investigation outcome into an Investigation
Record with full operational metrics. This closes the feedback
loop — future investigations can check records to avoid duplicate
work, and the metrics corpus enables trend analysis.

The agent reads ticket state (ledger, plan, evaluation result,
anomaly context) and EventBus data (token counts, cost, timing)
to populate the record.
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

from .prompts import SYNTHESIS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class SynthesisAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(
            agent_name="synthesis-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
        )

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return SYNTHESIS_SYSTEM_PROMPT

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        anomaly = cf.get("anomaly_context", {})
        ledger = cf.get("investigation_ledger", [])
        eval_result = cf.get("evaluation_result", {})
        plan = cf.get("execution_plan", {})
        change_ctx = cf.get("change_context")

        content = (
            f"## Investigation Complete\n\n**Summary:** {ticket.get('summary', '')}\n\n"
        )

        if anomaly:
            content += (
                f"**Anomaly Context:**\n"
                f"```json\n{json.dumps(anomaly, indent=2)}\n"
                f"```\n\n"
            )

        if eval_result:
            content += (
                f"**Evaluation Result:**\n"
                f"- Decision: {eval_result.get('decision', '')}\n"
                f"- Gate: {eval_result.get('convergence_gate', '')}\n"
                f"- Confidence: {eval_result.get('confidence', 0)}\n"
            )
            if eval_result.get("root_cause_summary"):
                content += f"- Root Cause: {eval_result['root_cause_summary']}\n"
            content += "\n"

        if ledger:
            content += "**Investigation Ledger:**\n"
            for entry in ledger:
                content += (
                    f"- Iteration {entry.get('iteration', '?')}: "
                    f'hypothesis="{entry.get("hypothesis", "")[:60]}" '
                    f'conclusion="{entry.get("conclusion", "")[:60]}" '
                    f"info_gain={entry.get('info_gain', 0.0)}\n"
                )
            content += "\n"

        steps = plan.get("steps", [])
        if steps:
            content += (
                f"**Plan:** {len(steps)} steps, "
                f"{sum(1 for s in steps if s.get('status') == 'completed')} completed\n\n"
            )

        if change_ctx:
            content += (
                f"**Change Context:**\n"
                f"```json\n{json.dumps(change_ctx, indent=2)}\n"
                f"```\n\n"
            )

        content += (
            "Produce the final Investigation Record summary "
            "by calling submit_synthesis_result.\n"
        )

        return [{"role": "user", "content": content}]

    async def run(self, ticket_id: str) -> None:
        synth_server = str(Path(__file__).with_name("server.py"))
        ir_server = str(Path(__file__).parent.parent / "investigation" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(synth_server, name="synthesis")

        try:
            await mcp.connect(ir_server, name="investigation-records")
        except Exception:
            logger.info(
                "[synthesis-agent] Investigation records MCP "
                "not available — record will not be persisted"
            )

        self._mcp = mcp
        mcp_tools = await mcp.list_tools()
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

        root_cause = result.get("root_cause_summary", "")
        confidence = result.get("confidence", 0.0)
        outcome = result.get("convergence_outcome", "")
        change_class = result.get("change_classification", "")
        causal_commits_str = result.get("causal_commits", "")
        change_summary = result.get("change_summary", "")
        build_id = result.get("build_id", "")
        notes = result.get("notes", "")

        causal_commits = (
            [c.strip() for c in causal_commits_str.split(",") if c.strip()]
            if causal_commits_str
            else []
        )

        # Collect operational metrics from ticket and EventBus
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        metrics = self._collect_operational_metrics(ticket_id, cf)

        # Create the Investigation Record via MCP
        anomaly = cf.get("anomaly_context", {})
        record_created = False
        if self._mcp and anomaly:
            try:
                record_result = await self._mcp.call_tool(
                    "create_investigation_record",
                    {
                        "subsystem": anomaly.get("subsystem", ""),
                        "metric": anomaly.get("metric", ""),
                        "direction": anomaly.get("direction", "degrading"),
                        "platform": anomaly.get("platform", ""),
                        "magnitude": anomaly.get("magnitude", ""),
                        "root_cause_summary": root_cause,
                        "confidence": confidence,
                        "convergence_outcome": outcome,
                        "build_id": build_id,
                    },
                )
                record_created = True
                logger.info(
                    f"[synthesis-agent] Investigation Record created: {record_result}"
                )
            except Exception:
                logger.exception(
                    "[synthesis-agent] Failed to create Investigation Record"
                )

        # Persist synthesis result on ticket
        synth_fields: dict[str, Any] = {
            "synthesis_result": {
                "root_cause_summary": root_cause,
                "confidence": confidence,
                "convergence_outcome": outcome,
                "change_classification": change_class,
                "causal_commits": causal_commits,
                "change_summary": change_summary,
                "build_id": build_id,
                "record_created": record_created,
                "operational_metrics": metrics,
                "notes": notes,
            },
        }
        await self._update_fields(ticket_id, synth_fields)

        summary = (
            f"**Investigation Synthesis Complete**\n\n"
            f"- **Outcome:** {outcome}\n"
            f"- **Confidence:** {confidence}\n"
            f"- **Root Cause:** {root_cause or '(unknown)'}\n"
            f"- **Record Created:** {record_created}\n"
        )
        if change_class:
            summary += f"- **Classification:** {change_class}\n"
        if causal_commits:
            summary += f"- **Commits:** {', '.join(causal_commits)}\n"
        if metrics:
            rc = metrics.get("resource_consumption", {})
            summary += (
                f"- **Tokens:** "
                f"{rc.get('llm_tokens_total', 0):,}\n"
                f"- **Cost:** "
                f"${rc.get('estimated_cost_usd', 0):.4f}\n"
                f"- **Iterations:** "
                f"{len(cf.get('investigation_ledger', []))}\n"
            )

        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "awaiting_teardown",
            comment=(f"Synthesis complete: {outcome} (confidence={confidence})"),
        )

    def _collect_operational_metrics(
        self,
        ticket_id: str,
        custom_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Collect operational metrics from ticket and EventBus.

        Populates the OperationalMetrics fields for the
        Investigation Record.
        """
        ledger = custom_fields.get("investigation_ledger", [])
        eval_result = custom_fields.get("evaluation_result", {})
        plan = custom_fields.get("execution_plan", {})

        # Info gain trajectory from ledger
        info_gains = [entry.get("info_gain", 0.0) for entry in ledger]

        # Count provision cycles from plan steps
        provision_cycles = sum(
            1
            for s in plan.get("steps", [])
            if s.get("agent_type") == "benchmark" and s.get("status") == "completed"
        )

        # Token/cost from EventBus
        resource = {}
        if self._events:
            usage = self._events.get_cumulative_usage(ticket_id)
            try:
                from providers.cost import (
                    estimate_cumulative_cost,
                )

                cost = estimate_cumulative_cost(usage)
            except ImportError:
                cost = 0.0

            resource = {
                "llm_tokens_total": usage.get("total_tokens", 0),
                "llm_invocations": usage.get("llm_calls", 0),
                "estimated_cost_usd": round(cost, 6),
            }

        return {
            "provision_cycles": provision_cycles,
            "convergence_outcome": eval_result.get("convergence_gate", ""),
            "info_gain_trajectory": info_gains,
            "stall_events": 0,
            "resource_consumption": resource,
        }
