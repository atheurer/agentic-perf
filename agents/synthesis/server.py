"""FastMCP server for the Synthesis agent.

Provides the submit_synthesis_result tool. Investigation Record
tools are served by the investigation-records MCP server.
"""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

mcp = FastMCP("synthesis-agent")


@mcp.tool()
async def submit_synthesis_result(
    root_cause_summary: str,
    confidence: float = 0.0,
    convergence_outcome: str = "",
    change_classification: str = "",
    causal_commits: str = "",
    change_summary: str = "",
    build_id: str = "",
    notes: str = "",
) -> str:
    """Submit the synthesis result for Investigation Record creation.

    root_cause_summary: what was found (or what's known if stalled)
    confidence: 0.0-1.0 final confidence
    convergence_outcome: ISOLATION | ENTROPY_STALL | MAX_ITERATIONS |
        EXPECTED_REGRESSION | MANUAL_INTERRUPTION
    change_classification: ISOLATION | EXPECTED_REGRESSION (if known)
    causal_commits: comma-separated commit hashes (if known)
    change_summary: description of causal change (if known)
    build_id: the build that was investigated
    notes: additional context
    """
    return f"Synthesis recorded: {convergence_outcome}"


if __name__ == "__main__":
    mcp.run()
