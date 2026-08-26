"""FastMCP server for ticket workspace scratchpad tools.

Exposes generic data inspection primitives (jq_query, grep_file, read_file_slice,
list_workspace_files) over stdio. The ticket workspace directory is resolved
using TICKET_ID from the environment.

Run directly:  python agents/workspace/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from providers.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

mcp = FastMCP("workspace")

_manager: WorkspaceManager | None = None


def _get_manager() -> WorkspaceManager:
    global _manager
    if _manager is None:
        ticket_id = os.environ.get("TICKET_ID", "")
        ws_dir_env = os.environ.get("WORKSPACE_DIR")
        _manager = WorkspaceManager(ticket_id=ticket_id, workspace_dir=ws_dir_env)
    return _manager


@mcp.tool()
async def jq_query(
    file_ref: str,
    filter: str,
    limit: int = 50,
) -> str:
    """Execute a jq filter expression on a structured JSON workspace file.

    Args:
        file_ref: workspace:// URI or relative filename (e.g. 'workspace://cdm_ts.json')
        filter: jq expression (e.g. '.uperf_100.values' or '.[] | {name, status}')
        limit: max list items to return in result (default 50)
    """
    manager = _get_manager()
    res = manager.jq_query(file_ref, filter, limit=limit)
    return json.dumps(res, indent=2)


@mcp.tool()
async def grep_file(
    file_ref: str,
    pattern: str,
    max_lines: int = 50,
    context_lines: int = 0,
    case_insensitive: bool = True,
) -> str:
    """Search for string or regex pattern in a workspace text file.

    Args:
        file_ref: workspace:// URI or relative filename (e.g. 'workspace://ethtool_stats.txt')
        pattern: regex pattern to search for
        max_lines: maximum matching lines to return (default 50)
        context_lines: lines of context before and after each match (default 0)
        case_insensitive: case-insensitive match (default True)
    """
    manager = _get_manager()
    res = manager.grep_file(
        file_ref,
        pattern,
        max_lines=max_lines,
        context_lines=context_lines,
        case_insensitive=case_insensitive,
    )
    return json.dumps(res, indent=2)


@mcp.tool()
async def read_file_slice(
    file_ref: str,
    offset_bytes: int = 0,
    max_bytes: int = 4096,
    start_line: int = 1,
    max_lines: int = 50,
) -> str:
    """Read a slice/chunk of a workspace file by lines or bytes.

    Args:
        file_ref: workspace:// URI or relative filename
        offset_bytes: byte offset to start reading from
        max_bytes: maximum bytes to read
        start_line: 1-indexed starting line number
        max_lines: number of lines to read (if set, uses line slicing instead of byte slicing)
    """
    manager = _get_manager()
    res = manager.read_file_slice(
        file_ref,
        offset_bytes=offset_bytes,
        max_bytes=max_bytes,
        start_line=start_line,
        max_lines=max_lines,
    )
    return json.dumps(res, indent=2)


@mcp.tool()
async def list_workspace_files() -> str:
    """List all files in the current ticket's scratchpad workspace."""
    manager = _get_manager()
    files = manager.list_files()
    return json.dumps({"status": "ok", "files": files, "count": len(files)}, indent=2)


@mcp.tool()
async def generate_chart_from_workspace(
    file_ref: str,
    title: str = "Performance Metric Chart",
    chart_type: str = "bar",
    harness: str | None = None,
    output_name: str | None = None,
    x_field: str | None = None,
    y_field: str | None = None,
    group_by: str | None = None,
    metric: str | None = None,
    breakout: str | None = None,
    unit: str | None = None,
    max_points: int = 60,
    jq_filter: str | None = None,
) -> str:
    """Extract and generate a declarative Chart.js/Recharts performance chart from a workspace JSON/CSV file.

    Args:
        file_ref: workspace:// URI or relative filename (e.g. 'workspace://cdm_metric_1.json')
        title: chart title (e.g. 'Server CPU Busy % by Core')
        chart_type: chart type ('bar', 'line', 'doughnut')
        harness: optional benchmark harness name ('crucible', 'kube-burner', etc.)
        output_name: optional output JSON filename under workspace://charts/
        x_field: field name for X-axis labels (e.g. 'cpu', 'threads', 'time')
        y_field: field name for Y-axis numeric values (e.g. 'busy_pct', 'gbps', 'iops')
        group_by: field name to group multiple series by (e.g. 'host', 'queue')
        metric: metric name for CDM/Crucible data (e.g. 'mpstat::Busy-CPU')
        unit: metric unit (e.g. 'Gbps', '%', 'IOPS', 'ms')
        max_points: maximum data points to plot for line charts (default 60)
        jq_filter: optional in-flight jq expression to filter file content before charting
    """
    manager = _get_manager()
    res = manager.generate_chart(
        file_ref=file_ref,
        title=title,
        chart_type=chart_type,
        harness=harness,
        output_name=output_name,
        x_field=x_field,
        y_field=y_field,
        group_by=group_by,
        metric=metric,
        breakout=breakout,
        unit=unit,
        max_points=max_points,
        jq_filter=jq_filter,
    )
    return json.dumps(res, indent=2)


if __name__ == "__main__":
    mcp.run()
