"""FastMCP server for triage agent tools.

Exposes benchmark discovery tools (list, details, resolve) over stdio.
The SkillProvider is constructed from environment variables so credentials
and provider internals never cross the LLM boundary.

Run directly:  python agents/triage/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import build_skill_provider

mcp = FastMCP("triage-agent")

_skill_provider = None


def _get_provider():
    global _skill_provider
    if _skill_provider is None:
        _skill_provider = build_skill_provider()
    return _skill_provider


# Non-arcaflow benchmarks available as standalone tools
# on the benchmark agent's MCP server. This list enables
# triage to discover benchmarks outside the arcaflow
# plugin ecosystem. Each entry is surfaced in
# list_benchmarks, resolve_benchmark, and
# get_benchmark_details alongside arcaflow plugins.
_STANDALONE_BENCHMARKS = [
    {
        "name": "boot-time",
        "description": (
            "Boot time analysis — reboots a remote system "
            "multiple times and collects kernel, initrd, and "
            "userspace timing metrics per cycle. Uses "
            "boot-time-analysis-tools. NO provisioning "
            "step — the benchmark tool installs "
            "dependencies on the SUT automatically via SSH. "
            "Do NOT tell provisioning to install any "
            "boot-time packages."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "harness": "boot-time",
    },
]


@mcp.tool()
async def list_benchmarks() -> str:
    """List all available benchmark suites with their descriptions and supported parameters."""
    sp = _get_provider()
    benchmarks = await sp.list_benchmarks()
    result = [
        {
            "name": b.name,
            "description": b.description,
            "roles": b.roles,
            "min_hosts": b.min_hosts,
            "harness": b.harness,
        }
        for b in benchmarks
    ]
    # Include standalone (non-arcaflow) benchmarks
    result.extend(_STANDALONE_BENCHMARKS)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_benchmark_details(name: str) -> str:
    """Get detailed information about a specific benchmark suite including supported parameters and endpoint types."""
    # Check standalone benchmarks first
    for sb in _STANDALONE_BENCHMARKS:
        if name == sb["name"]:
            return json.dumps(sb, indent=2)
    sp = _get_provider()
    b = await sp.get_benchmark(name)
    if b is None:
        return json.dumps({"error": f"Benchmark '{name}' not found"})
    return json.dumps(
        {
            "name": b.name,
            "description": b.description,
            "supported_params": b.supported_params,
            "roles": b.roles,
            "min_hosts": b.min_hosts,
            "harness": b.harness,
        },
        indent=2,
    )


@mcp.tool()
async def resolve_benchmark(
    description: str,
    workload_type: str = "",
    harness: str = "",
) -> str:
    """Given a natural language description of what the user wants to test, find the best matching benchmark suite. Returns the suite name or null if no match."""
    sp = _get_provider()
    reqs: dict[str, Any] = {
        "description": description,
        "workload_type": workload_type,
    }
    if harness:
        reqs["harness"] = harness

    # Check standalone benchmarks first
    desc_lower = description.lower()
    for sb in _STANDALONE_BENCHMARKS:
        if sb["name"] in desc_lower or any(
            kw in desc_lower for kw in ("boot time", "boot-time", "reboot")
        ):
            return json.dumps(
                {
                    "matched_suite": sb["name"],
                    "harness": sb["harness"],
                    "harnesses": [sb["harness"]],
                }
            )

    result = await sp.resolve_benchmark(reqs)
    if result is None:
        return json.dumps({"matched_suite": None})

    capable: list[dict[str, Any]] = []
    if hasattr(sp, "find_capable_harnesses"):
        capable = await sp.find_capable_harnesses(result)
    harnesses_list = [c["harness"] for c in capable]

    response: dict[str, Any] = {
        "matched_suite": result,
        "harnesses": harnesses_list,
    }
    if len(harnesses_list) == 1:
        response["harness"] = harnesses_list[0]
        response["note"] = (
            f"Only '{harnesses_list[0]}' provides this benchmark "
            f"— set harness directive to '{harnesses_list[0]}'"
        )
    elif len(harnesses_list) > 1:
        response["note"] = (
            f"Multiple harnesses offer this benchmark: {harnesses_list}. "
            "Set harness directive if the user specified one, "
            "otherwise the default harness will be used."
        )
    return json.dumps(response, indent=2)


if __name__ == "__main__":
    mcp.run()
