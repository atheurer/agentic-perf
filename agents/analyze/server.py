"""FastMCP server for the analysis agent.

Provides tools for querying prior ticket data and submitting
analysis results. External MCP tools (Domain MCP) are connected
separately via the external_mcp_servers config.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

mcp = FastMCP("analyze-agent")

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_STORE_URL = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
_AUTH_TOKEN = os.environ.get("AGENTIC_PERF_API_TOKEN", "")


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if _AUTH_TOKEN:
        h["Authorization"] = f"Bearer {_AUTH_TOKEN}"
    return h


@mcp.tool()
async def read_skill(category: str, filename: str) -> str:
    """Read an investigation methodology skill document.

    Skill docs contain domain-specific knowledge for
    investigating anomalies (e.g., boot-time phase analysis,
    known patterns, measurement model).

    Args:
        category: Skill category (e.g., 'boot-time', 'jumpstarter').
        filename: Filename within the skill directory.
    """
    skill_path = SKILLS_DIR / category / filename
    if not skill_path.is_file():
        return json.dumps(
            {
                "found": False,
                "message": f"Skill not found: {category}/{filename}",
            },
        )
    resolved = skill_path.resolve()
    if not str(resolved).startswith(str(SKILLS_DIR.resolve())):
        return json.dumps({"found": False, "message": "Invalid path"})
    return json.dumps(
        {
            "found": True,
            "filename": filename,
            "content": skill_path.read_text(),
        },
    )


@mcp.tool()
async def list_skill_docs(category: str) -> str:
    """List available skill documents for a category.

    Args:
        category: Skill category (e.g., 'boot-time', 'jumpstarter').
    """
    skill_dir = SKILLS_DIR / category
    if not skill_dir.is_dir():
        return json.dumps(
            {
                "found": False,
                "message": f"No skills for category: {category}",
            },
        )
    files = [f.name for f in sorted(skill_dir.iterdir()) if f.suffix == ".md"]
    return json.dumps({"category": category, "files": files})


@mcp.tool()
async def get_ticket_results(ticket_id: str) -> str:
    """Retrieve benchmark results, KPIs, and evaluation findings
    from a prior ticket. Use this to compare results across
    investigations or reference earlier work."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_STORE_URL}/api/v1/tickets/{ticket_id}",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return json.dumps({"error": f"Ticket {ticket_id} not found"})
        resp.raise_for_status()
        ticket = resp.json()

    cf = ticket.get("custom_fields", {})
    result = {
        "ticket_id": ticket_id,
        "summary": ticket.get("summary", ""),
        "status": ticket.get("status", ""),
        "harness": cf.get("directives", {}).get("harness", ""),
        "benchmark_result": cf.get("benchmark_result"),
        "evaluation_result": cf.get("evaluation_result"),
        "analysis_result": cf.get("analysis_result"),
        "verdict": cf.get("verdict"),
        "review_summary": cf.get("review_summary"),
    }
    return json.dumps(result, default=str)


@mcp.tool()
async def search_tickets(
    harness: str = "",
    board_type: str = "",
    status: str = "closed",
    limit: int = 20,
) -> str:
    """Search for prior tickets by harness, board type, or status.
    Returns matching ticket IDs with summaries and key results.
    Use this to find relevant prior investigations for comparison.

    Note: fetches all tickets then filters in Python. Acceptable
    for current scale; will need server-side filtering if the
    ticket store grows large."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_STORE_URL}/api/v1/tickets",
            headers=_headers(),
        )
        resp.raise_for_status()
        tickets = resp.json()

    matches = []
    for t in tickets:
        if status and t.get("status") != status:
            continue
        cf = t.get("custom_fields", {})
        directives = cf.get("directives", {})
        if harness and directives.get("harness") != harness:
            continue
        if board_type:
            selector = directives.get("board_selector", "")
            if board_type not in selector:
                continue
        br = cf.get("benchmark_result", {})
        matches.append(
            {
                "ticket_id": t.get("id", ""),
                "summary": t.get("summary", ""),
                "status": t.get("status", ""),
                "harness": directives.get("harness", ""),
                "verdict": cf.get("verdict"),
                "kpis": br.get("kpis") if isinstance(br, dict) else None,
            }
        )
        if len(matches) >= limit:
            break

    return json.dumps(
        {"count": len(matches), "tickets": matches},
        default=str,
    )


@mcp.tool()
async def submit_analysis_result(
    conclusive: bool,
    finding: str,
    evidence: str = "",
    root_cause: str = "",
    benchmark_needed_reason: str = "",
    benchmark_needed_params: str = "",
) -> str:
    """Submit analysis findings. Set conclusive=true if the data
    answers the question (ticket advances to review). Set
    conclusive=false if new benchmark data is needed (ticket
    advances to hardware provisioning).

    benchmark_needed_params is a JSON string of suggested
    benchmark parameters (e.g. '{"samples": 50, "duration": 60}').
    """
    result: dict = {
        "conclusive": conclusive,
        "finding": finding,
        "evidence": evidence,
    }
    if root_cause:
        result["root_cause"] = root_cause
    if not conclusive and benchmark_needed_reason:
        result["benchmark_needed"] = {
            "reason": benchmark_needed_reason,
        }
        if benchmark_needed_params:
            try:
                result["benchmark_needed"]["suggested_params"] = json.loads(
                    benchmark_needed_params
                )
            except (json.JSONDecodeError, TypeError):
                pass
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run()
