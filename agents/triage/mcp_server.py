from __future__ import annotations

import json
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
                    "min_hosts": {
                        "type": "integer",
                        "description": "Minimum endpoint hosts required (from benchmark roles)",
                    },
                    "roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required host roles (e.g. ['client'] or ['client', 'server'])",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes about the triage",
                    },
                },
                "required": ["parsed_specs", "hypothesis", "benchmark_suite", "absent_suite", "min_hosts", "roles"],
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
        }

    async def resolve_benchmark(
        description: str, workload_type: str = ""
    ) -> dict:
        result = await skill_provider.resolve_benchmark(
            {"description": description, "workload_type": workload_type}
        )
        return {"matched_suite": result}

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    return {
        "list_benchmarks": list_benchmarks,
        "get_benchmark_details": get_benchmark_details,
        "resolve_benchmark": resolve_benchmark,
        "request_clarification": request_clarification,
    }
