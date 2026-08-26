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


if __name__ == "__main__":
    mcp.run()
