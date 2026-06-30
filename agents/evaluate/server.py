"""FastMCP server for the Evaluate agent.

Provides the submit_evaluation_result tool. Investigation Record
tools and infra tools are served by their respective MCP servers
(connected separately).
"""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from providers.llm.base import ToolDefinition

mcp = FastMCP("evaluate-agent")


@mcp.tool()
async def submit_evaluation_result(
    decision: str,
    convergence_gate: str = "",
    confidence: float = 0.0,
    updated_hypothesis: str = "",
    info_gain: float = 0.0,
    params_rationale: str = "",
    next_params: str = "",
    root_cause_summary: str = "",
    notes: str = "",
) -> str:
    """Submit the convergence evaluation decision.

    decision: "loop_plan" | "loop_provision" | "converged" | "stalled"
    convergence_gate: which gate fired (isolation | entropy_stall |
        expected_regression) — required when converged/stalled
    confidence: 0.0-1.0 confidence in the assessment
    updated_hypothesis: refined hypothesis for the next iteration
        (required when looping)
    info_gain: information gained this iteration (0.0-1.0)
    params_rationale: why these parameters for the next iteration
    next_params: JSON string of parameters for the next benchmark
        step (when looping)
    root_cause_summary: final root cause (when converged)
    notes: additional context
    """
    return f"Evaluation recorded: {decision}"


def get_evaluate_tools() -> list[ToolDefinition]:
    """Return tool definitions for local handler registration."""
    return [
        ToolDefinition(
            name="submit_evaluation_result",
            description=("Submit the convergence evaluation decision."),
            input_schema={
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": [
                            "loop_plan",
                            "loop_provision",
                            "converged",
                            "stalled",
                        ],
                        "description": ("The convergence decision"),
                    },
                    "convergence_gate": {
                        "type": "string",
                        "description": (
                            "Which gate fired: isolation, "
                            "entropy_stall, expected_regression"
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": ("0.0-1.0 confidence in the assessment"),
                    },
                    "updated_hypothesis": {
                        "type": "string",
                        "description": ("Refined hypothesis for next iteration"),
                    },
                    "info_gain": {
                        "type": "number",
                        "description": ("Information gained (0.0-1.0)"),
                    },
                    "params_rationale": {
                        "type": "string",
                        "description": ("Why these params for next iteration"),
                    },
                    "next_params": {
                        "type": "string",
                        "description": ("JSON params for next benchmark step"),
                    },
                    "root_cause_summary": {
                        "type": "string",
                        "description": ("Final root cause (when converged)"),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional context",
                    },
                },
                "required": ["decision"],
            },
        ),
    ]


if __name__ == "__main__":
    mcp.run()
