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
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import (
    build_repo_cache,
    build_skill_provider,
    build_ssh_from_ticket,
)

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
        return json.dumps(
            {
                "status": "not_found",
                "path": str(skill_path),
                "available": available,
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "filename": filename,
            "content": skill_path.read_text(),
        }
    )


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
async def read_run_results(
    controller: str,
    run_id: str,
    file_path: str = "",
    max_bytes: int = 4000,
) -> str:
    """Read benchmark run result files from the controller.

    Two modes:
    - Listing mode (no file_path): returns file paths and sizes for
      all result/tool files in the run directory. Use this first to
      see what is available, then request specific files.
    - Reading mode (with file_path): returns the contents of one file.
      Automatically decompresses .xz files. Use max_bytes to control
      how much data is returned (default 4000 for a preview; call
      again with a larger value if you need more).
    """
    await _ensure_init()

    find_result = await _ssh.run(
        controller,
        f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
        timeout=15,
    )
    run_dir = find_result.stdout.strip() if find_result.exit_code == 0 else ""
    if not run_dir:
        return json.dumps(
            {
                "run_id": run_id,
                "status": "not_found",
                "message": f"No run directory found matching {run_id}",
            }
        )

    if not file_path:
        find_cmd = (
            f"find {run_dir} -type f "
            f"\\( -name '*.xz' -o -name '*.csv' -o -name '*.json' "
            f"-o -name '*.txt' -o -name 'result*' \\) "
            f"! -path '*/roadblock/*' ! -path '*/msgs/*' "
            f"! -path '*/workshop/*' "
            f"2>/dev/null"
            f" | head -100"
        )
        find_files = await _ssh.run(controller, find_cmd, timeout=20)
        raw_paths = (
            find_files.stdout.strip().split("\n")
            if find_files.exit_code == 0 and find_files.stdout.strip()
            else []
        )

        if not raw_paths:
            return json.dumps(
                {
                    "status": "no_files_found",
                    "run_id": run_id,
                    "run_dir": run_dir,
                    "message": "No result files found in run directory.",
                }
            )

        size_cmd = "stat --printf='%s %n\\n' " + " ".join(f"'{p}'" for p in raw_paths)
        size_result = await _ssh.run(controller, size_cmd, timeout=20)
        files = []
        if size_result.exit_code == 0 and size_result.stdout.strip():
            for line in size_result.stdout.strip().split("\n"):
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    try:
                        files.append(
                            {
                                "path": parts[1],
                                "size_bytes": int(parts[0]),
                            },
                        )
                    except ValueError:
                        files.append({"path": parts[1], "size_bytes": -1})
        else:
            files = [{"path": p, "size_bytes": -1} for p in raw_paths]

        return json.dumps(
            {
                "status": "ok",
                "run_id": run_id,
                "run_dir": run_dir,
                "files": files,
                "message": (
                    "Use read_run_results with a specific file_path to "
                    "read contents. Files ending in .xz are automatically "
                    "decompressed. Start with max_bytes=4000 (default) to "
                    "preview, then request more if needed."
                ),
            }
        )

    if not file_path.startswith("/var/lib/crucible/run"):
        return json.dumps(
            {
                "status": "error",
                "message": (
                    "Access denied. Only paths under "
                    "/var/lib/crucible/run are permitted."
                ),
            }
        )

    limit_bytes = min(max(max_bytes, 100), 50000)
    is_xz = file_path.endswith(".xz")
    if is_xz:
        cmd = f"xzcat '{file_path}' 2>/dev/null | head -c {limit_bytes}"
    else:
        cmd = f"head -c {limit_bytes} '{file_path}'"

    read_result = await _ssh.run(controller, cmd, timeout=30)
    if read_result.exit_code != 0:
        return json.dumps(
            {
                "status": "error",
                "run_id": run_id,
                "file_path": file_path,
                "message": f"Failed to read {file_path}",
                "stderr": (read_result.stderr[:500] if read_result.stderr else ""),
            }
        )

    content = read_result.stdout or ""
    file_size_cmd = f"stat --printf='%s' '{file_path}'"
    file_size_result = await _ssh.run(
        controller,
        file_size_cmd,
        timeout=10,
    )
    try:
        file_size = int(file_size_result.stdout.strip())
    except (ValueError, AttributeError):
        file_size = -1

    return json.dumps(
        {
            "status": "ok",
            "run_id": run_id,
            "file_path": file_path,
            "is_compressed": is_xz,
            "bytes_read": len(content),
            "file_size": file_size,
            "truncated": len(content) >= limit_bytes,
            "content": content,
        }
    )


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
        return json.dumps(
            {
                "run_id": run_id,
                "status": "not_found",
                "message": f"No run directory found matching {run_id}",
            }
        )

    summary_path = f"{run_dir}/run/result-summary.json"
    result = await _ssh.run(controller, f"cat {summary_path}", timeout=30)
    if result.exit_code != 0:
        return json.dumps(
            {
                "run_id": run_id,
                "status": "error",
                "run_dir": run_dir,
                "message": f"{summary_path} not found — run may still be indexing",
                "stderr": result.stderr[:500] if result.stderr else "",
            }
        )

    try:
        return json.dumps(json.loads(result.stdout))
    except json.JSONDecodeError:
        return json.dumps(
            {
                "run_id": run_id,
                "status": "error",
                "message": "result-summary.json exists but is not valid JSON",
                "raw": result.stdout[:2000],
            }
        )


async def cdm_api_request(
    controller: str,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    port: int = 3000,
) -> str:
    """Internal helper — use cdm_api_requests (plural) to make CDM queries."""
    await _ensure_init()

    if method == "GET":
        cmd = f"curl --silent --show-error --fail -X GET http://localhost:{port}{path}"
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
        return json.dumps(
            {
                "status": "error",
                "method": method,
                "path": path,
                "exit_code": result.exit_code,
                "error": result.stderr or "",
            }
        )

    try:
        return json.dumps(json.loads(result.stdout))
    except json.JSONDecodeError:
        return json.dumps(
            {
                "status": "error",
                "method": method,
                "path": path,
                "raw_output": result.stdout or "",
                "error": "Response is not valid JSON",
            }
        )


@mcp.tool()
async def cdm_api_requests(
    controller: str,
    requests: list[dict],
    port: int = 3000,
) -> str:
    """Make multiple CDM API requests concurrently in one call. Each entry needs 'label', 'method', and 'path'; optionally 'body' for POST. Use this instead of calling cdm_api_request repeatedly to save iterations — e.g. fetch uperf Gbps, mpstat CPU, and tcp-window cwnd for all samples in one call."""
    import asyncio as _asyncio

    await _ensure_init()

    async def _one(req: dict) -> tuple[str, dict]:
        label = req.get("label", req.get("path", "?"))
        raw = await cdm_api_request(
            controller=controller,
            path=req.get("path", ""),
            method=req.get("method", "POST"),
            body=req.get("body"),
            port=port,
        )
        try:
            return label, json.loads(raw)
        except Exception:
            return label, {"raw": raw}

    results = await _asyncio.gather(
        *[_one(r) for r in requests], return_exceptions=True
    )
    out = {}
    for item in results:
        if isinstance(item, Exception):
            out["error"] = {"status": "error", "error": str(item)}
        else:
            label, data = item
            out[label] = data
    return json.dumps(out)


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

    return json.dumps(
        {
            "status": "ok",
            "run_ids": run_ids,
            "metric_name": metric_name,
            "results": results,
        }
    )


# ---------------------------------------------------------------------------
# Config tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_review_config(harness_name: str) -> str:
    """Get the review/results-retrieval configuration for a benchmark harness. Returns how to find and interpret results for this harness — result storage method, directory paths, API details, and guidance notes. Call this first to learn how to access results for the harness that ran the benchmark."""
    await _ensure_init()

    if not _skill_provider:
        return json.dumps(
            {
                "status": "error",
                "message": "No skill provider configured — cannot look up review config",
            }
        )
    try:
        config = await _skill_provider.get_all_private_config(harness_name)
    except Exception as e:
        return json.dumps(
            {
                "status": "error",
                "message": f"Failed to load private config for {harness_name}: {e}",
            }
        )
    review = config.get("review", {})
    if not review:
        execution = config.get("execution", {})
        return json.dumps(
            {
                "status": "no_review_config",
                "harness": harness_name,
                "message": (
                    f"No 'review' section found in {harness_name} private-skills config. "
                    f"Try using read_run_results with the results directory from the "
                    f"run file or execution config."
                ),
                "results_dir_pattern": execution.get("results_dir_pattern", ""),
                "execution_keys": list(execution.keys()),
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "harness": harness_name,
            "review_config": review,
        }
    )


if __name__ == "__main__":
    mcp.run()
