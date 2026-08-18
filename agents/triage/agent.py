from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition

from .prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_KNOWN_SCOPED_CONTEXT_KEYS = frozenset(
    {"shared", "resource", "provision", "benchmark", "review"}
)

_LOCAL_TOOLS = [
    ToolDefinition(
        name="request_clarification",
        description=(
            "Ask the user for clarification when the test request is "
            "ambiguous or missing critical information. This will pause "
            "the ticket and wait for human input."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the user",
                }
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="submit_triage_result",
        description=(
            "Submit the triage result when analysis is complete. "
            "Call this tool with your findings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "parsed_specs": {
                    "type": "object",
                    "description": (
                        "Hardware/software specs extracted from the request"
                    ),
                },
                "hypothesis": {
                    "type": "string",
                    "description": "What the user wants to prove or disprove",
                },
                "benchmark_suite": {
                    "type": "string",
                    "description": "The resolved benchmark suite name",
                },
                "absent_suite": {
                    "type": "boolean",
                    "description": (
                        "True if no automation suite covers this benchmark"
                    ),
                },
                "required_hosts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "roles": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Roles this host serves (e.g. "
                                    '["controller"], ["client"], '
                                    '["controller", "client"])'
                                ),
                            },
                            "nic_speed": {
                                "type": ["integer", "string"],
                                "description": (
                                    "Required NIC speed in Gbps (e.g. 25, '100Gbps')"
                                ),
                            },
                            "min_cores": {
                                "type": "integer",
                                "description": "Minimum CPU cores",
                            },
                            "min_memory_gb": {
                                "type": "integer",
                                "description": "Minimum RAM in GB",
                            },
                            "os": {
                                "type": "string",
                                "description": "OS requirement (e.g. 'RHEL9')",
                            },
                        },
                        "required": ["roles"],
                    },
                    "description": (
                        "Every host needed for the test, each with its "
                        "roles and optional hardware requirements. "
                        "Always include a controller. A host can serve "
                        "multiple roles (e.g. controller + client). "
                        "Attach hardware specs the user requested to "
                        "the relevant host entries. "
                        "Example: [{roles: [controller], min_memory_gb: 16}, "
                        "{roles: [client], nic_speed: 25, os: 'RHEL9'}, "
                        "{roles: [server], nic_speed: 25, os: 'RHEL9'}]"
                    ),
                },
                "directives": {
                    "type": "object",
                    "description": (
                        "Operational directives extracted from the user's "
                        "request. Only include directives the user explicitly "
                        "or clearly implied. Omit any directive that was not "
                        "mentioned."
                    ),
                    "properties": {
                        "on_existing_install": {
                            "type": "string",
                            "enum": [
                                "reinstall",
                                "update",
                                "skip",
                                "ask_user",
                            ],
                            "description": (
                                "What to do if the harness is already "
                                "installed. 'reinstall' = uninstall then "
                                "clean install, 'update' = update in place, "
                                "'skip' = use existing installation, "
                                "'ask_user' = ask the user what to do."
                            ),
                        },
                        "harness": {
                            "type": "string",
                            "description": (
                                "Which benchmark harness to use (e.g. "
                                "'crucible', 'zathras'). Only set if the "
                                "user explicitly names a harness."
                            ),
                        },
                        "user_pre_run_approval": {
                            "type": "boolean",
                            "description": (
                                "Whether to ask the user for approval "
                                "before starting the benchmark run. "
                                "Defaults to true if not specified. Set "
                                "to false if the user says something like "
                                "'don't ask me for approval' or 'just run it'."
                            ),
                        },
                        "host_cleanup": {
                            "type": "string",
                            "enum": ["required", "skip"],
                            "description": (
                                "Whether to clean up SSH keys and harness "
                                "installations from hosts during teardown. "
                                "Default: required."
                            ),
                        },
                        "endpoint_type": {
                            "type": "string",
                            "enum": ["remotehosts", "kube"],
                            "description": (
                                "Endpoint type for the benchmark. "
                                "'remotehosts' runs directly on "
                                "bare-metal/VM hosts. 'kube' runs in "
                                "Kubernetes pods (K3s installed on the "
                                "controller). Set to 'kube' when user "
                                "mentions Kubernetes, K8s, pods, "
                                "containers, or cloud-native."
                            ),
                        },
                    },
                    "additionalProperties": True,
                },
                "execution_plan": {
                    "type": "array",
                    "description": (
                        "Optional multi-step execution plan. Include "
                        "when the user's request requires multiple "
                        "benchmark runs or different infrastructure "
                        "per iteration. Each step specifies an "
                        "agent_type and params. The final step "
                        "should be 'review'."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "enum": [
                                    "teardown",
                                    "resource",
                                    "provision",
                                    "benchmark",
                                    "review",
                                ],
                            },
                            "params": {
                                "type": "object",
                                "description": (
                                    "Step-specific params. For benchmark: "
                                    "label and mv_params overrides. "
                                    "For resource: required_hosts list "
                                    "and optional directives overrides. "
                                    "For teardown/provision/review: empty."
                                ),
                            },
                        },
                        "required": ["agent_type", "params"],
                    },
                },
                "scoped_context": {
                    "type": "object",
                    "description": (
                        "Agent-scoped context partitioned from the user's "
                        "request. Each key is an agent role (resource, "
                        "provisioning, benchmark, review) or 'shared' for "
                        "context relevant to all agents. Values are natural "
                        "language summaries of the portions of the request "
                        "relevant to that agent. Agent-prefixed directives "
                        "(e.g., 'provision agent: install nmap-ncat') go in "
                        "the corresponding agent's section."
                    ),
                    "properties": {
                        "shared": {
                            "type": "string",
                            "description": (
                                "Context relevant to all agents "
                                "(environment, general constraints, "
                                "test objective summary)"
                            ),
                        },
                        "resource": {
                            "type": "string",
                            "description": (
                                "Context for the resource agent "
                                "(host requirements, provider preferences, "
                                "instance types, regions)"
                            ),
                        },
                        "provision": {
                            "type": "string",
                            "description": (
                                "Context for the provision agent "
                                "(installation instructions, package "
                                "requirements, setup directives)"
                            ),
                        },
                        "benchmark": {
                            "type": "string",
                            "description": (
                                "Context for the benchmark agent "
                                "(test parameters, workload details, "
                                "connectivity requirements, run approval)"
                            ),
                        },
                        "review": {
                            "type": "string",
                            "description": (
                                "Context for the review agent "
                                "(analysis expectations, comparison "
                                "criteria, reporting requirements)"
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                "reference_tickets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ticket IDs referenced for comparison or "
                        "context (e.g. ['PERF-ABC123', 'PERF-DEF456']). "
                        "Set when the user asks to compare or reference "
                        "prior investigation results."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Additional notes about the triage",
                },
            },
            "required": [
                "parsed_specs",
                "hypothesis",
                "benchmark_suite",
                "absent_suite",
                "required_hosts",
            ],
        },
    ),
]


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

        local_tools = list(_LOCAL_TOOLS)

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

    def _has_external_data_tools(self) -> bool:
        """Check if external MCP data tools are configured."""
        from orchestrator.config import _load_config_file

        try:
            config = _load_config_file()
        except Exception:
            return False
        servers = config.get("external_mcp_servers", [])
        for srv in servers:
            agents = srv.get("agents", {})
            if "analyze" in agents or "gathering_context" in agents:
                return True
        return False

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Performance Test Request\n\n"
            f"**Ticket ID:** {ticket['id']}\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
        )

        # Tell triage whether external data tools are available
        has_data_tools = self._has_external_data_tools()
        cf = ticket.get("custom_fields", {})
        has_anomaly = bool(cf.get("anomaly_context"))
        ref_tickets = cf.get("reference_tickets", [])

        content += "\n## Data Analysis Capability\n\n"
        if has_data_tools:
            content += (
                "External data tools ARE available. You CAN "
                "include an `analyze` step in the execution plan "
                "when the investigation would benefit from "
                "analyzing existing data before provisioning "
                "hardware.\n"
            )
        else:
            content += (
                "External data tools are NOT available. Do NOT "
                "include an `analyze` step — use the standard "
                "benchmark-first plan.\n"
            )
        if has_anomaly:
            content += (
                "\nThis ticket has anomaly_context (alert-triggered). "
                "It will route through gathering_context for dedup "
                "before reaching the execution plan.\n"
            )
        if ref_tickets:
            content += (
                f"\nReference tickets provided: {ref_tickets}. "
                "Include an analyze step for cross-ticket "
                "comparison.\n"
            )

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        cf = ticket.get("custom_fields", {})
        verbatim_directives = cf.get("verbatim_directives") or {}
        if verbatim_directives:
            content += "\n## Pre-parsed Verbatim Directives\n\n"
            content += (
                "The following directives were extracted verbatim from the "
                "ticket description and will be delivered directly to each "
                "target agent. Do NOT summarize or paraphrase these in "
                "`scoped_context` — your `scoped_context` entries should "
                "contain only supplemental context that these blocks do not "
                "already cover.\n\n"
            )
            for target, text in verbatim_directives.items():
                content += f"**agent:{target}:**\n```\n{text}\n```\n\n"

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

        required_hosts = result.get("required_hosts", [])
        directives = result.get("directives", {})
        # Backward compat: top-level host_cleanup moves into directives
        if "host_cleanup" in result and "host_cleanup" not in directives:
            directives["host_cleanup"] = result["host_cleanup"]
        # Preserve user-provided directives that triage
        # didn't set. The user may have specified
        # image_version or other operational parameters
        # in the ticket's custom_fields.
        # User directives take precedence over triage's
        # — triage fills gaps, doesn't override.
        # Also check top-level custom_fields for directives
        # that the user set outside the directives dict
        # (e.g., image_version, system_config).
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        user_directives = cf.get("directives", {})
        if user_directives:
            merged = dict(directives)
            merged.update(user_directives)
            directives = merged
        # Promote top-level custom_fields that belong
        # in directives but weren't placed there by
        # the ticket creator.
        _PROMOTABLE = (
            "image_version",
            "serial_capture",
        )
        for key in _PROMOTABLE:
            if key in cf and key not in directives:
                directives[key] = cf[key]
        fields: dict[str, Any] = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
            "benchmark_suite": result.get("benchmark_suite", ""),
            "absent_suite": result.get("absent_suite", False),
            "required_hosts": required_hosts,
            "directives": directives,
        }

        scoped_context = result.get("scoped_context")
        if scoped_context and isinstance(scoped_context, dict):
            unknown = set(scoped_context) - _KNOWN_SCOPED_CONTEXT_KEYS
            if unknown:
                logger.warning(
                    "scoped_context contains unknown keys %s — dropping",
                    sorted(unknown),
                )
                scoped_context = {
                    k: v
                    for k, v in scoped_context.items()
                    if k in _KNOWN_SCOPED_CONTEXT_KEYS
                }
            fields["scoped_context"] = scoped_context

        reference_tickets = result.get("reference_tickets")
        if reference_tickets and isinstance(reference_tickets, list):
            fields["reference_tickets"] = reference_tickets

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
            # The resource agent validates SSH, inventories hosts, and
            # populates assigned_hardware_ips.  If the LLM omitted it
            # (e.g. user-provided hosts), prepend one so the pipeline
            # doesn't skip host validation.  "analyze" is the only
            # valid non-resource first step (data-only investigation).
            if steps[0]["agent_type"] not in ("resource", "analyze"):
                steps.insert(
                    0,
                    {
                        "id": 0,
                        "agent_type": "resource",
                        "status": "in_progress",
                        "params": {},
                        "results": {},
                    },
                )
                for i, s in enumerate(steps):
                    s["id"] = i
                    s["status"] = "in_progress" if i == 0 else "pending"
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
            "provision": "provision",
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
            f"- **Required Hosts:** {len(required_hosts)} ({', '.join('+'.join(h.get('roles', ['?'])) for h in required_hosts)})\n"
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
            # Only "resource" and "analyze" have valid transitions
            # from triage_pending.  Any other step type (e.g. the LLM
            # skipping "resource" and putting "provision" first) must
            # funnel through awaiting_hardware so the resource agent
            # still validates hosts and populates assigned_hardware_ips.
            _TRIAGE_EXIT_STATUSES = {
                "resource": "awaiting_hardware",
                "analyze": "analyzing",
            }
            first_step_type = steps[0]["agent_type"]
            first_status = _TRIAGE_EXIT_STATUSES.get(
                first_step_type,
                "awaiting_hardware",
            )
            await self._transition_ticket(
                ticket_id,
                first_status,
                comment=f"Triage complete, starting plan step 0: {first_step_type}",
            )
