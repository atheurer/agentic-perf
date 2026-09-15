"""FastMCP server for the Gathering Context agent.

Provides the submit_gathering_context_result tool for the agent
to report its dedup decision. Investigation Record tools are
served by the investigation-records MCP server (connected
separately).
"""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from providers.llm.base import ToolDefinition

mcp = FastMCP("gathering-context")


@mcp.tool()
async def submit_gathering_context_result(
    decision: str,
    matched_investigation_id: str = "",
    match_confidence: float = 0.0,
    match_rationale: str = "",
    notes: str = "",
) -> str:
    """Submit the dedup gate decision.

    decision: "MATCH_FOUND" or "NO_MATCH"
    matched_investigation_id: ID of the matched record (if any)
    match_confidence: 0.0-1.0 confidence in the match
    match_rationale: explanation of why this matches (or doesn't)
    notes: any additional context for the next agent
    """
    return f"Decision recorded: {decision}"


def get_gathering_context_tools() -> list[ToolDefinition]:
    """Return tool definitions for local handler registration."""
    return [
        ToolDefinition(
            name="submit_gathering_context_result",
            description=(
                'Submit the dedup gate decision. decision: "MATCH_FOUND" or "NO_MATCH"'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["MATCH_FOUND", "NO_MATCH"],
                        "description": (
                            "Whether a matching open Investigation Record was found"
                        ),
                    },
                    "matched_investigation_id": {
                        "type": "string",
                        "description": ("ID of the matched record (if any)"),
                    },
                    "match_confidence": {
                        "type": "number",
                        "description": ("0.0-1.0 confidence in the match"),
                    },
                    "match_rationale": {
                        "type": "string",
                        "description": ("Explanation of why this matches"),
                    },
                    "notes": {
                        "type": "string",
                        "description": ("Additional context for the next agent"),
                    },
                },
                "required": ["decision"],
            },
        ),
    ]


if __name__ == "__main__":
    mcp.run()
