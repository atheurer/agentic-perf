from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import get_triage_tools
from .prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMES = frozenset(
    {"list_benchmarks", "get_benchmark_details", "resolve_benchmark"}
)


class TriageAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._ticket_id: str | None = None

        local_tools = [t for t in get_triage_tools() if t.name not in _MCP_TOOL_NAMES]

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        local_handlers = {
            "request_clarification": _request_clarification,
        }

        super().__init__(
            agent_name="triage-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers=local_handlers,
            event_bus=event_bus,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        triage_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(triage_server, name="triage")
        await mcp.connect(infra_server, name="infra")
        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return TRIAGE_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Performance Test Request\n\n"
            f"**Ticket ID:** {ticket['id']}\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
        )

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            await self._add_comment(
                ticket_id, "Triage agent could not produce structured output."
            )
            return

        roles = result.get("roles", [])
        min_hosts = result.get("min_hosts", 1)
        directives = result.get("directives", {})
        # Backward compat: top-level host_cleanup moves into directives
        if "host_cleanup" in result and "host_cleanup" not in directives:
            directives["host_cleanup"] = result["host_cleanup"]
        # Preserve user-provided directives that triage
        # didn't set. The user may have specified
        # image_version or other operational parameters
        # in the ticket's custom_fields.directives.
        ticket = await self._get_ticket(ticket_id)
        user_directives = ticket.get("custom_fields", {}).get("directives", {})
        if user_directives:
            merged = dict(user_directives)
            merged.update(directives)
            directives = merged
        fields: dict[str, Any] = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
            "benchmark_suite": result.get("benchmark_suite", ""),
            "absent_suite": result.get("absent_suite", False),
            "required_roles": roles,
            "min_hosts": min_hosts,
            "directives": directives,
        }

        scoped_context = result.get("scoped_context")
        if scoped_context and isinstance(scoped_context, dict):
            fields["scoped_context"] = scoped_context

        # Detect anomaly context from the triage result or
        # ticket description. This field drives code-enforced
        # routing to the investigation path.
        anomaly_ctx = result.get("anomaly_context")
        if not anomaly_ctx:
            # Check if the ticket description has anomaly
            # indicators that triage recognized.
            ticket = await self._get_ticket(ticket_id)
            desc = ticket.get("description", "").lower()
            hypothesis = fields.get("hypothesis", "").lower()
            anomaly_markers = (
                "regression",
                "anomaly",
                "watchdog",
                "degradation",
                "deviation",
                "failure",
                "isolate",
                "intermittent",
            )
            if any(m in desc for m in anomaly_markers) and any(
                m in hypothesis for m in anomaly_markers
            ):
                anomaly_ctx = {
                    "source": "triage_detection",
                    "hypothesis": fields.get("hypothesis", ""),
                }
        if anomaly_ctx:
            fields["anomaly_context"] = anomaly_ctx

        # Fleet investigation: test all available devices
        # of a type and compare results. Requires
        # investigation context (enforced by providers/fleet.py).
        if result.get("fleet_investigation"):
            if not anomaly_ctx:
                # Fleet implies investigation — auto-set
                # anomaly context if not already detected.
                anomaly_ctx = {
                    "source": "fleet_investigation",
                    "hypothesis": fields.get("hypothesis", ""),
                }
                fields["anomaly_context"] = anomaly_ctx
            fields["fleet_investigation"] = {
                "enabled": True,
                "tested_hosts": [],
            }

        # Every ticket gets a full-lifecycle execution plan covering
        # resource allocation through teardown. The LLM should
        # produce this, but if it doesn't, we build a default.
        raw_plan = result.get("execution_plan")
        if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 0:
            steps = []
            for i, s in enumerate(raw_plan):
                steps.append(
                    {
                        "id": i,
                        "agent_type": s.get("agent_type", "benchmark"),
                        "status": "in_progress" if i == 0 else "pending",
                        "params": s.get("params", {}),
                        "results": {},
                    }
                )
        else:
            # Default full-lifecycle plan
            steps = [
                {
                    "id": 0,
                    "agent_type": "resource",
                    "status": "in_progress",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 1,
                    "agent_type": "provision",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 2,
                    "agent_type": "benchmark",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 3,
                    "agent_type": "review",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
                {
                    "id": 4,
                    "agent_type": "teardown",
                    "status": "pending",
                    "params": {},
                    "results": {},
                },
            ]
        fields["execution_plan"] = {
            "current_step": 0,
            "run_ids": [],
            "steps": steps,
        }

        # Apply step 0's overrides directly — _apply_step_overrides
        # handles this for subsequent steps, but step 0 runs before
        # any plan advancement.
        first_params = steps[0].get("params", {})
        first_type = steps[0]["agent_type"]

        # Step 0's required_hosts override the ticket-level list
        if first_type == "resource" and first_params.get("required_hosts"):
            fields["required_hosts"] = first_params["required_hosts"]

        # Clear the first step's scoped_context section so the
        # agent relies on structured data instead of multi-iteration
        # text.
        agent_key_map = {
            "resource": "resource",
            "provision": "provisioning",
            "benchmark": "benchmark",
            "review": "review",
        }
        first_key = agent_key_map.get(first_type)
        if (
            first_key
            and "scoped_context" in fields
            and first_key in fields["scoped_context"]
        ):
            del fields["scoped_context"][first_key]

        await self._update_fields(ticket_id, fields)

        summary = (
            f"**Triage Complete**\n\n"
            f"- **Hypothesis:** {fields['hypothesis']}\n"
            f"- **Benchmark Suite:** {fields['benchmark_suite']}\n"
            f"- **Required Hosts:** {min_hosts} ({', '.join(roles) if roles else 'unknown'})\n"
            f"- **Absent Suite:** {fields['absent_suite']}\n"
        )
        step_types = [s["agent_type"] for s in steps]
        summary += (
            f"- **Execution Plan:** {len(steps)} steps ({' → '.join(step_types)})\n"
        )
        if directives:
            summary += f"- **Directives:** {', '.join(f'{k}={v}' for k, v in directives.items())}\n"
        if fields.get("scoped_context"):
            agents_with_context = [
                k
                for k in fields["scoped_context"]
                if k != "shared" and fields["scoped_context"].get(k)
            ]
            if agents_with_context:
                summary += f"- **Scoped Context:** {', '.join(agents_with_context)}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)

        # Route based on whether anomaly_context is present.
        # Set by alert seeds, CLI, or API — not inferred by
        # the LLM. Code enforces the routing invariant.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        if cf.get("anomaly_context"):
            await self._transition_ticket(
                ticket_id,
                "gathering_context",
                comment=(
                    "Triage complete, anomaly context present"
                    " — routing to investigation"
                ),
            )
        else:
            # Transition to the first plan step's target status
            first_step_type = steps[0]["agent_type"]
            first_status = self._PLAN_AGENT_STATUS.get(
                first_step_type,
                "awaiting_hardware",
            )
            await self._transition_ticket(
                ticket_id,
                first_status,
                comment=f"Triage complete, starting plan step 0: {first_step_type}",
            )
