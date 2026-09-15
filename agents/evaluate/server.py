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


@mcp.tool()
async def list_benchmark_artifacts(
    output_dir: str,
) -> str:
    """List available diagnostic artifacts in a benchmark output directory.

    Use this to discover what logs, serial captures, and result
    files are available for a benchmark run. Then use
    read_benchmark_artifact to examine specific files.

    Args:
        output_dir: Path to the benchmark output directory
            (from benchmark_results.output_dir on the ticket).
    """
    import json

    path = Path(output_dir)
    if not path.exists():
        return json.dumps({"error": f"Directory not found: {output_dir}"})

    artifacts: list[dict[str, object]] = []
    for f in sorted(path.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(path))
            artifacts.append(
                {
                    "path": rel,
                    "size_bytes": f.stat().st_size,
                    "type": _classify_artifact(rel),
                }
            )

    return json.dumps(
        {
            "output_dir": output_dir,
            "count": len(artifacts),
            "artifacts": artifacts,
        },
        indent=2,
    )


@mcp.tool()
async def read_benchmark_artifact(
    output_dir: str,
    filename: str,
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Read a specific diagnostic artifact from a benchmark output directory.

    Use list_benchmark_artifacts first to discover available files,
    then read specific ones to investigate failures.

    Args:
        output_dir: Path to the benchmark output directory.
        filename: Relative path within the output directory
            (from list_benchmark_artifacts results).
        offset: Line number to start reading from (0-based).
        limit: Maximum number of lines to return (default 200).
    """
    import json

    path = Path(output_dir) / filename

    # Safety: check traversal before existence to avoid
    # leaking information about files outside the dir.
    try:
        path.resolve().relative_to(Path(output_dir).resolve())
    except ValueError:
        return json.dumps({"error": "Path traversal not allowed"})

    if not path.exists():
        return json.dumps({"error": f"File not found: {filename}"})

    try:
        content = path.read_text(errors="replace")
        lines = content.splitlines()
        total = len(lines)
        selected = lines[offset : offset + limit]
        return json.dumps(
            {
                "filename": filename,
                "total_lines": total,
                "offset": offset,
                "limit": limit,
                "lines_returned": len(selected),
                "has_more": (offset + limit) < total,
                "content": "\n".join(selected),
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to read {filename}: {e}"})


def _classify_artifact(path: str) -> str:
    """Classify an artifact by its filename pattern."""
    p = path.lower()
    if "serial" in p:
        return "serial_capture"
    if "journal" in p:
        return "journal_log"
    if "trace" in p:
        return "trace_log"
    if "summary" in p and p.endswith(".json"):
        return "timing_summary"
    if "boot_time_logs" in p:
        return "boot_timing_data"
    if "collection_status" in p:
        return "collection_status"
    if "merged" in p:
        return "merged_results"
    if "metadata" in p:
        return "system_metadata"
    if p.endswith(".json"):
        return "json_data"
    if p.endswith(".log"):
        return "log"
    return "other"


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
                            "loop_analyze",
                            "converged",
                            "stalled",
                        ],
                        "description": (
                            "The convergence decision. Use "
                            "loop_analyze to go back and query "
                            "more existing data instead of "
                            "running another benchmark."
                        ),
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
        ToolDefinition(
            name="list_benchmark_artifacts",
            description=(
                "List available diagnostic artifacts "
                "(logs, serial captures, result files) "
                "in a benchmark output directory. Use "
                "this to discover what data is available "
                "for failure investigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Path to benchmark output "
                            "directory (from "
                            "benchmark_results.output_dir)"
                        ),
                    },
                },
                "required": ["output_dir"],
            },
        ),
        ToolDefinition(
            name="read_benchmark_artifact",
            description=(
                "Read a specific diagnostic artifact "
                "from a benchmark output directory. "
                "Use list_benchmark_artifacts first to "
                "discover available files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": ("Path to benchmark output directory"),
                    },
                    "filename": {
                        "type": "string",
                        "description": ("Relative path within the output directory"),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Line number to start from (0-based, default 0)"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": ("Max lines to return (default 200)"),
                    },
                },
                "required": [
                    "output_dir",
                    "filename",
                ],
            },
        ),
    ]


if __name__ == "__main__":
    mcp.run()
