"""FastMCP server for review agent tools.

Exposes result-retrieval, skill/doc, and config tools over stdio.
SSH credentials are resolved from the ticket via _ensure_init(),
never passed as tool parameters — this is a security improvement
over the original mcp_server.py which exposed ssh_key_path to the LLM.

Run directly:  python agents/review/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import build_skill_provider, build_ssh_from_ticket, build_repo_cache

logger = logging.getLogger(__name__)

mcp = FastMCP("review-agent")

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Module-level globals — lazily initialized by _ensure_init()
_initialized = False
_ssh = None
_skill_provider = None
_repo_cache = None
_ticket: dict[str, Any] = {}


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _skill_provider, _repo_cache, _ticket
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    _skill_provider = build_skill_provider()
    try:
        _repo_cache = build_repo_cache()
    except Exception:
        _repo_cache = None
    _initialized = True


# ---------------------------------------------------------------------------
# Skill / Doc tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_skill(harness: str, filename: str) -> str:
    """Read a skill document containing lessons learned from prior benchmark runs. These may contain guidance on interpreting results for specific harnesses or benchmarks."""
    await _ensure_init()
    skill_path = SKILLS_DIR / harness / filename
    if not skill_path.is_file():
        available = []
        harness_dir = SKILLS_DIR / harness
        if harness_dir.is_dir():
            available = [f.name for f in harness_dir.glob("*.md")]
        return json.dumps({
            "status": "not_found",
            "path": str(skill_path),
            "available": available,
        })
    return json.dumps({
        "status": "ok",
        "filename": filename,
        "content": skill_path.read_text(),
    })


@mcp.tool()
async def list_harness_docs(harness: str) -> str:
    """List documentation files available for a benchmark harness. Use this to discover reference material about result formats and interpretation."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"status": "error", "message": "No repo cache configured"})
    docs = _repo_cache.list_docs(harness, subdirs=["docs", "config"])
    return json.dumps({"harness": harness, "docs": docs})


@mcp.tool()
async def read_harness_doc(harness: str, doc_path: str) -> str:
    """Read a documentation file from a benchmark harness repository. Use this to learn about result formats, metric interpretation, or any other harness-specific details."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"status": "error", "message": "No repo cache configured"})
    content = _repo_cache.read_file(harness, doc_path)
    if content is None:
        return json.dumps({"status": "not_found", "harness": harness, "path": doc_path})
    return json.dumps({"status": "ok", "path": doc_path, "content": content[:15000]})


# ---------------------------------------------------------------------------
# SSH-based result tools (ssh_key_path removed — resolved from ticket)
# ---------------------------------------------------------------------------


@mcp.tool()
async def retrieve_results(
    controller: str,
    results_dir: str,
    file_pattern: str = "",
    harness: str = "",
) -> str:
    """Retrieve benchmark result files from the controller host via SSH. Finds result files in the specified directory and returns their contents. Use the results directory from the review config or run file."""
    await _ensure_init()

    if not file_pattern:
        find_cmd = (
            f"find {results_dir} -maxdepth 3 "
            f"\\( -name '*.csv' -o -name '*.json' -o -name 'result*' "
            f"-o -name 'summary*' -o -name '*.out' \\) "
            f"-type f 2>/dev/null | head -50"
        )
    else:
        find_cmd = (
            f"find {results_dir} -maxdepth 3 -name '{file_pattern}' "
            f"-type f 2>/dev/null | head -50"
        )

    find_result = await _ssh.run(controller, find_cmd, timeout=15)
    if find_result.exit_code != 0 or not find_result.stdout.strip():
        ls_result = await _ssh.run(
            controller,
            f"ls -laR {results_dir} 2>/dev/null | head -100",
            timeout=15,
        )
        return json.dumps({
            "status": "no_files_found",
            "results_dir": results_dir,
            "pattern": file_pattern or "(default)",
            "directory_listing": ls_result.stdout[:3000] if ls_result.stdout else "",
            "message": (
                "No matching result files found. The directory listing is "
                "included — use it to identify the correct file paths and "
                "call retrieve_results again with a more specific pattern."
            ),
        })

    files = find_result.stdout.strip().split("\n")
    contents = {}
    total_size = 0
    max_total = 50000

    for fpath in files:
        if total_size >= max_total:
            contents[fpath] = "(skipped — total output limit reached)"
            continue
        result = await _ssh.run(
            controller,
            f"head -c 10000 '{fpath}'",
            timeout=15,
        )
        if result.exit_code == 0 and result.stdout:
            contents[fpath] = result.stdout
            total_size += len(result.stdout)
        else:
            contents[fpath] = f"(read error: {result.stderr[:200] if result.stderr else 'empty'})"

    return json.dumps({
        "status": "ok",
        "results_dir": results_dir,
        "files_found": len(files),
        "contents": contents,
    })


@mcp.tool()
async def get_run_summary(
    controller: str,
    run_id: str,
    harness: str = "crucible",
) -> str:
    """Get a structured JSON summary of a crucible benchmark run. Reads the result-summary.json from the crucible run directory. Only applicable when the harness is crucible — check get_review_config first."""
    await _ensure_init()

    find_result = await _ssh.run(
        controller,
        f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
        timeout=15,
    )
    run_dir = find_result.stdout.strip() if find_result.exit_code == 0 else ""
    if not run_dir:
        return json.dumps({
            "run_id": run_id,
            "status": "not_found",
            "message": f"No run directory found matching {run_id}",
        })

    summary_path = f"{run_dir}/run/result-summary.json"
    result = await _ssh.run(controller, f"cat {summary_path}", timeout=30)
    if result.exit_code != 0:
        return json.dumps({
            "run_id": run_id,
            "status": "error",
            "run_dir": run_dir,
            "message": f"{summary_path} not found — run may still be indexing",
            "stderr": result.stderr[:500] if result.stderr else "",
        })

    try:
        return json.dumps(json.loads(result.stdout))
    except json.JSONDecodeError:
        return json.dumps({
            "run_id": run_id,
            "status": "error",
            "message": "result-summary.json exists but is not valid JSON",
            "raw": result.stdout[:2000],
        })


@mcp.tool()
async def cdm_api_request(
    controller: str,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    port: int = 3000,
) -> str:
    """Make an HTTP request to the CDM query server on the controller. Only applicable when the harness is crucible and the review config indicates cdm_api as the results method."""
    await _ensure_init()

    if method == "GET":
        cmd = (
            f"curl --silent --show-error --fail "
            f"-X GET http://localhost:{port}{path}"
        )
    else:
        body_json = json.dumps(body or {})
        cmd = (
            f"curl --silent --show-error --fail "
            f"-X POST http://localhost:{port}{path} "
            f"-H 'Content-Type: application/json' "
            f"-d '{body_json}'"
        )

    result = await _ssh.run(controller, cmd, timeout=60)
    if result.exit_code != 0:
        return json.dumps({
            "status": "error",
            "method": method,
            "path": path,
            "exit_code": result.exit_code,
            "error": result.stderr or "",
        })

    try:
        return json.dumps(json.loads(result.stdout))
    except json.JSONDecodeError:
        return json.dumps({
            "status": "error",
            "method": method,
            "path": path,
            "raw_output": result.stdout or "",
            "error": "Response is not valid JSON",
        })


@mcp.tool()
async def compare_results(
    controller: str,
    run_ids: list[str],
    metric_name: str = "",
    port: int = 3000,
) -> str:
    """Compare metrics between two or more benchmark runs via the CDM API. Only applicable for crucible runs."""
    await _ensure_init()

    results = {}
    for rid in run_ids:
        body_json = json.dumps({"runIds": [rid]})
        api_result = await _ssh.run(
            controller,
            f"curl --silent --show-error --fail "
            f"-X POST http://localhost:{port}/api/v1/iterations/metric-values "
            f"-H 'Content-Type: application/json' "
            f"-d '{body_json}'",
            timeout=60,
        )
        if api_result.exit_code != 0:
            results[rid] = {
                "status": "error",
                "exit_code": api_result.exit_code,
                "error": api_result.stderr or "",
            }
        else:
            try:
                results[rid] = json.loads(api_result.stdout)
            except json.JSONDecodeError:
                results[rid] = {
                    "status": "error",
                    "error": "Response is not valid JSON",
                    "raw_output": api_result.stdout[:2000] if api_result.stdout else "",
                }

    return json.dumps({
        "status": "ok",
        "run_ids": run_ids,
        "metric_name": metric_name,
        "results": results,
    })


# ---------------------------------------------------------------------------
# Config tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_review_config(harness_name: str) -> str:
    """Get the review/results-retrieval configuration for a benchmark harness. Returns how to find and interpret results for this harness — result storage method, directory paths, API details, and guidance notes. Call this first to learn how to access results for the harness that ran the benchmark."""
    await _ensure_init()

    if not _skill_provider:
        return json.dumps({
            "status": "error",
            "message": "No skill provider configured — cannot look up review config",
        })
    try:
        config = await _skill_provider.get_all_private_config(harness_name)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to load private config for {harness_name}: {e}",
        })
    review = config.get("review", {})
    if not review:
        execution = config.get("execution", {})
        return json.dumps({
            "status": "no_review_config",
            "harness": harness_name,
            "message": (
                f"No 'review' section found in {harness_name} private-skills config. "
                f"Try using retrieve_results with the results directory from the "
                f"run file or execution config."
            ),
            "results_dir_pattern": execution.get("results_dir_pattern", ""),
            "execution_keys": list(execution.keys()),
        })
    return json.dumps({
        "status": "ok",
        "harness": harness_name,
        "review_config": review,
    })


if __name__ == "__main__":
    mcp.run()
