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

from agents.mcp_audit import create_ticket_mcp
from agents.server_utils import build_skill_provider_async, read_skill_documents
from agents.skill_gateway import SKILL_GATEWAY_TOOL_DESCRIPTION, skill_context_gateway
from providers.skills.base import HARNESS_ALIASES

mcp = create_ticket_mcp("analyze-agent")

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_STORE_URL = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
_AUTH_TOKEN = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
_skill_provider = None


async def _get_skill_provider():
    global _skill_provider
    if _skill_provider is None:
        _skill_provider = await build_skill_provider_async(
            ticket_id=os.environ.get("TICKET_ID", ""),
            resolve_source=False,
            skill_phase="analyze",
        )
    return _skill_provider


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if _AUTH_TOKEN:
        h["Authorization"] = f"Bearer {_AUTH_TOKEN}"
    return h


@mcp.tool(description=SKILL_GATEWAY_TOOL_DESCRIPTION)
async def get_skill_context(
    subject: str,
    operation: str = "bootstrap",
    ref: str = "",
    path: str = "",
    from_ref: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
    cursor: str = "",
) -> str:
    """Retrieve guidance through server-owned subject and source bindings."""
    return await skill_context_gateway(
        await _get_skill_provider(),
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="analyze-agent",
        phase="analyze",
        subject=subject,
        operation=operation,
        ref=ref,
        path=path,
        from_ref=from_ref,
        query=query,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
        cursor=cursor,
        local_skills_dir=SKILLS_DIR,
    )


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read one or more skill documents in one call. Each entry in docs must be a dict with 'harness' (or 'category') and 'filename' (e.g. [{'harness': 'boot-time', 'filename': 'investigation-methodology.md'}]). Skill docs contain domain-specific knowledge for investigating anomalies."""
    allowed = []
    rejected = []
    for doc in docs:
        harness = doc.get("harness") or doc.get("category", "")
        if isinstance(harness, str) and harness.strip().lower() == "crucible":
            rejected.append(
                {
                    "found": False,
                    "harness": harness,
                    "filename": doc.get("filename") or doc.get("name", ""),
                    "message": (
                        "Use get_skill_context(subject='harness/crucible', "
                        "operation='bootstrap')."
                    ),
                }
            )
        else:
            allowed.append(doc)
    return json.dumps(rejected + read_skill_documents(SKILLS_DIR, allowed))


@mcp.tool()
async def list_skill_docs(category: str) -> str:
    """List available skill documents for a category.

    Args:
        category: Skill category (e.g., 'boot-time', 'jumpstarter').
    """
    category_root = (
        category.strip().strip("/").removeprefix("skills/").split("/", 1)[0].lower()
    )
    if category_root == "crucible":
        return json.dumps(
            {
                "found": False,
                "message": "Use get_skill_context(subject='harness/crucible', operation='bootstrap').",
            }
        )
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
    """Retrieve benchmark results, KPIs, evaluation findings, and
    artifact paths from a prior ticket. Use this to compare results
    across investigations or reference earlier work.

    Always returns artifact_base_path and artifact_run_dirs when
    artifacts exist on disk, regardless of whether output_dir was
    persisted in custom_fields."""
    from providers.execution import AuditedAsyncHTTPClient

    async with AuditedAsyncHTTPClient(timeout=30) as client:
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
        # Artifact paths for cross-ticket analysis
        "output_dir": cf.get("output_dir"),
        "output_dirs": cf.get("output_dirs"),
    }

    # Always provide the artifact base path — even when output_dir
    # wasn't persisted, artifacts may exist on disk at the standard
    # location. Resolve and confine it beneath ARTIFACT_DIR so a
    # crafted ticket id cannot point outside the artifact tree.
    from paths import ARTIFACT_DIR

    try:
        artifact_root = Path(ARTIFACT_DIR).resolve()
        artifact_base = (artifact_root / ticket_id).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        artifact_root = artifact_base = None
    if (
        artifact_root is not None
        and artifact_base is not None
        and artifact_base.is_relative_to(artifact_root)
        and artifact_base.is_dir()
    ):
        result["artifact_base_path"] = str(artifact_base)
        # List run directories so the caller knows what's available
        try:
            run_dirs = sorted(str(d) for d in artifact_base.iterdir() if d.is_dir())
        except OSError:
            run_dirs = []
        if run_dirs:
            result["artifact_run_dirs"] = run_dirs

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
    from providers.execution import AuditedAsyncHTTPClient

    async with AuditedAsyncHTTPClient(timeout=30) as client:
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
        ticket_harness = directives.get("harness", "")
        # Normalize aliases so searches match across the
        # 'arcaflow' / 'arcaflow-plugins' boundary.
        ticket_harness = (
            ticket_harness.lower() if isinstance(ticket_harness, str) else ""
        )
        normalized_filter = harness.lower() if isinstance(harness, str) else ""
        ticket_harness = HARNESS_ALIASES.get(ticket_harness, ticket_harness)
        normalized_filter = HARNESS_ALIASES.get(
            normalized_filter,
            normalized_filter,
        )
        if harness and ticket_harness != normalized_filter:
            continue
        if board_type:
            selector = directives.get("board_selector", "") or cf.get(
                "board_selector", ""
            )
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


def _resolve_confined_artifact_dir(output_dir: str) -> Path | None:
    """Resolve ``output_dir`` and confine it beneath ``ARTIFACT_DIR``.

    ``list_benchmark_artifacts`` and ``read_benchmark_artifact`` are
    handed paths discovered from tickets, which must stay inside the
    artifact tree. Returns ``None`` when the path is outside the root.
    """
    from paths import ARTIFACT_DIR

    try:
        root = Path(ARTIFACT_DIR).resolve()
        resolved = Path(output_dir).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not resolved.is_relative_to(root):
        return None
    return resolved


@mcp.tool()
async def list_benchmark_artifacts(output_dir: str) -> str:
    """List available artifacts in a benchmark output directory.

    Use get_ticket_results to discover artifact paths for a ticket,
    then pass output_dir or artifact_run_dirs entries to this tool
    to list available files. Paths must be inside the artifact
    directory tree; other locations are rejected.

    Args:
        output_dir: Path to the benchmark output directory (must be
            inside the artifact tree).
    """
    path = _resolve_confined_artifact_dir(output_dir)
    if path is None:
        return json.dumps({"error": f"Directory not allowed: {output_dir}"})
    if not path.exists():
        return json.dumps({"error": f"Directory not found: {output_dir}"})

    artifacts = []
    for f in sorted(path.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(path))
            artifacts.append(
                {
                    "path": rel,
                    "size_bytes": f.stat().st_size,
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
    """Read a specific artifact file from a benchmark output directory.

    Args:
        output_dir: Path to the benchmark output directory.
        filename: Relative path within the output directory.
        offset: Line number to start reading from (0-based).
        limit: Maximum number of lines to return (default 200).
    """
    base = _resolve_confined_artifact_dir(output_dir)
    if base is None:
        return json.dumps({"error": "Path traversal not allowed"})
    path = base / filename

    try:
        path.resolve().relative_to(base)
    except ValueError:
        return json.dumps({"error": "Path traversal not allowed"})

    if not path.exists():
        return json.dumps({"error": f"File not found: {filename}"})

    try:
        content = path.read_text(errors="replace")
        lines = content.splitlines()
        total = len(lines)
        limit = min(limit, 500)
        selected = lines[offset : offset + limit]
        return json.dumps(
            {
                "filename": filename,
                "total_lines": total,
                "offset": offset,
                "lines_returned": len(selected),
                "content": "\n".join(selected),
            }
        )
    except Exception as e:
        return json.dumps({"error": f"Read failed: {e}"})


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
