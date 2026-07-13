from __future__ import annotations

from typing import Any

from providers.llm.base import ToolDefinition
from providers.skills.base import SkillProvider


def get_triage_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="list_benchmarks",
            description="List all available benchmark suites with their descriptions and supported parameters.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_benchmark_details",
            description="Get detailed information about a specific benchmark suite including supported parameters and endpoint types.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the benchmark suite (e.g. 'uperf', 'fio', 'trafficgen')",
                    }
                },
                "required": ["name"],
            },
        ),
        ToolDefinition(
            name="resolve_benchmark",
            description="Given a natural language description of what the user wants to test, find the best matching benchmark suite. Returns the suite name or null if no match.",
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Natural language description of the performance test requirements",
                    },
                    "workload_type": {
                        "type": "string",
                        "description": "Type of workload (e.g. 'network', 'storage', 'cpu', 'realtime')",
                    },
                },
                "required": ["description"],
            },
        ),
        ToolDefinition(
            name="request_clarification",
            description="Ask the user for clarification when the test request is ambiguous or missing critical information. This will pause the ticket and wait for human input.",
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
            description="Submit the triage result when analysis is complete. Call this tool with your findings.",
            input_schema={
                "type": "object",
                "properties": {
                    "parsed_specs": {
                        "type": "object",
                        "description": "Hardware/software specs extracted from the request",
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
                        "description": "True if no automation suite covers this benchmark",
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
                                        "Required NIC speed in Gbps "
                                        "(e.g. 25, '100Gbps')"
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
                                    "description": ("OS requirement (e.g. 'RHEL9')"),
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
                            "Operational directives extracted from the user's request. "
                            "Only include directives the user explicitly or clearly implied. "
                            "Omit any directive that was not mentioned."
                        ),
                        "properties": {
                            "on_existing_install": {
                                "type": "string",
                                "enum": ["reinstall", "update", "skip", "ask_user"],
                                "description": (
                                    "What to do if the harness is already installed. "
                                    "'reinstall' = uninstall then clean install, "
                                    "'update' = update in place, "
                                    "'skip' = use existing installation, "
                                    "'ask_user' = ask the user what to do."
                                ),
                            },
                            "harness": {
                                "type": "string",
                                "description": (
                                    "Which benchmark harness to use (e.g. 'crucible', 'zathras'). "
                                    "Only set if the user explicitly names a harness."
                                ),
                            },
                            "user_pre_run_approval": {
                                "type": "boolean",
                                "description": (
                                    "Whether to ask the user for approval before starting "
                                    "the benchmark run. Defaults to true if not specified. "
                                    "Set to false if the user says something like "
                                    "'don't ask me for approval' or 'just run it'."
                                ),
                            },
                            "host_cleanup": {
                                "type": "string",
                                "enum": ["required", "skip"],
                                "description": (
                                    "Whether to clean up SSH keys and harness installations "
                                    "from hosts during teardown. Default: required."
                                ),
                            },
                            "endpoint_type": {
                                "type": "string",
                                "enum": ["remotehosts", "kube"],
                                "description": (
                                    "Endpoint type for the benchmark. 'remotehosts' runs "
                                    "directly on bare-metal/VM hosts. 'kube' runs in "
                                    "Kubernetes pods (K3s installed on the controller). "
                                    "Set to 'kube' when user mentions Kubernetes, K8s, "
                                    "pods, containers, or cloud-native."
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
                            "provisioning": {
                                "type": "string",
                                "description": (
                                    "Context for the provisioning agent "
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
                        "additionalProperties": True,
                    },
                    "fleet_investigation": {
                        "type": "boolean",
                        "description": (
                            "Set to true when the user wants to "
                            "test ALL available devices of a type "
                            "and compare results across the fleet "
                            "(e.g., 'test all R-Car boards', "
                            "'which hosts have failures'). Fleet "
                            "investigations iterate through each "
                            "device one at a time via loop-back."
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


def create_triage_tool_handlers(
    skill_provider: SkillProvider,
    request_clarification_fn,
) -> dict[str, Any]:
    async def list_benchmarks() -> list[dict]:
        benchmarks = await skill_provider.list_benchmarks()
        return [
            {
                "name": b.name,
                "description": b.description,
                "roles": b.roles,
                "min_hosts": b.min_hosts,
                "harness": b.harness,
            }
            for b in benchmarks
        ]

    async def get_benchmark_details(name: str) -> dict | str:
        b = await skill_provider.get_benchmark(name)
        if b is None:
            return f"Benchmark '{name}' not found"
        return {
            "name": b.name,
            "description": b.description,
            "supported_params": b.supported_params,
            "roles": b.roles,
            "min_hosts": b.min_hosts,
            "harness": b.harness,
        }

    async def resolve_benchmark(
        description: str, workload_type: str = "", harness: str = ""
    ) -> dict:
        reqs: dict[str, Any] = {
            "description": description,
            "workload_type": workload_type,
        }
        if harness:
            reqs["harness"] = harness
        result = await skill_provider.resolve_benchmark(reqs)
        if result is None:
            return {"matched_suite": None}
        capable = []
        if hasattr(skill_provider, "find_capable_harnesses"):
            capable = await skill_provider.find_capable_harnesses(result)
        harnesses = [c["harness"] for c in capable]
        response: dict[str, Any] = {"matched_suite": result, "harnesses": harnesses}
        if len(harnesses) == 1:
            response["harness"] = harnesses[0]
            response["note"] = (
                f"Only '{harnesses[0]}' provides this benchmark — set harness directive to '{harnesses[0]}'"
            )
        elif len(harnesses) > 1:
            response["note"] = (
                f"Multiple harnesses offer this benchmark: {harnesses}. Set harness directive if the user specified one, otherwise the default harness will be used."
            )
        return response

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    return {
        "list_benchmarks": list_benchmarks,
        "get_benchmark_details": get_benchmark_details,
        "resolve_benchmark": resolve_benchmark,
        "request_clarification": request_clarification,
    }
