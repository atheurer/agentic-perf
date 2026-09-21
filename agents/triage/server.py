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

from agents.mcp_audit import create_ticket_mcp
from agents.server_utils import (
    build_skill_provider,
    read_skill_documents,
)

mcp = create_ticket_mcp("triage-agent")

SKILLS_DIR = Path(_project_root) / "skills"

_skill_provider = None


def _get_provider():
    global _skill_provider
    if _skill_provider is None:
        _skill_provider = build_skill_provider(
            resolve_source=False,
            catalog_only=True,
        )
    return _skill_provider


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read one or more skill documents in one call. Each entry in docs must be a dict with 'harness' and 'filename' (e.g. [{'harness': 'jumpstarter', 'filename': 'image-selection.md'}]). Skill docs contain domain-specific knowledge for directive resolution."""
    return json.dumps(read_skill_documents(SKILLS_DIR, docs))


@mcp.tool()
async def list_benchmarks() -> str:
    """List all available benchmark suites with their descriptions and supported parameters."""
    from providers.skills.catalog import list_benchmark_catalog

    result, _unavailable = await list_benchmark_catalog(_get_provider())
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_benchmark_details(name: str) -> str:
    """Get detailed information about a specific benchmark suite including supported parameters and endpoint types."""
    from providers.skills.catalog import get_catalog_benchmark

    detail = await get_catalog_benchmark(_get_provider(), name)
    if detail is None:
        return json.dumps({"error": f"Benchmark '{name}' not found"})
    # Arcaflow plugins: include the container image ref
    if detail["harness"] == "arcaflow-plugins":
        repo_name = f"arcaflow-plugin-{name.replace('arcaflow-', '')}"
        detail["container_image"] = f"quay.io/arcalot/{repo_name}"
        detail["execution_note"] = (
            "Runs as a container via podman. Community "
            "plugins from quay.io/arcalot are typically "
            "multi-arch (amd64 + arm64). Do NOT install "
            "the workload binary on the host."
        )
    return json.dumps(detail, indent=2)


@mcp.tool()
async def resolve_benchmark(
    description: str,
    workload_type: str = "",
    harness: str = "",
) -> str:
    """Given a natural language description of what the user wants to test, find the best matching benchmark suite. Returns the suite name or null if no match."""
    # Check standalone benchmarks first.
    from providers.skills.catalog import STANDALONE_BENCHMARKS

    desc_lower = description.lower()
    for sb in STANDALONE_BENCHMARKS:
        if sb.name in desc_lower or (harness and harness == sb.harness):
            return json.dumps(
                {
                    "matched_suite": sb.name,
                    "harness": sb.harness,
                    "harnesses": [sb.harness],
                    "note": (f"Only '{sb.harness}' provides this benchmark"),
                }
            )

    sp = _get_provider()
    reqs: dict[str, Any] = {
        "description": description,
        "workload_type": workload_type,
    }
    if harness:
        reqs["harness"] = harness

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
    requested_harness = harness.strip() if harness else ""
    if requested_harness:
        if requested_harness in harnesses_list:
            response["harness"] = requested_harness
            response["note"] = (
                f"Requested harness '{requested_harness}' provides this benchmark"
            )
        else:
            response["harness_unavailable"] = requested_harness
            response["note"] = (
                f"Requested harness '{requested_harness}' does not provide "
                f"benchmark '{result}'"
            )
    elif len(harnesses_list) == 1:
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


async def get_registered_tools():
    """Introspect this server's registered @mcp.tool() functions."""
    from providers.llm.base import ToolDefinition

    tools = await mcp.list_tools()
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or "",
            input_schema=t.parameters,
        )
        for t in tools
    ]


if __name__ == "__main__":
    mcp.run()
