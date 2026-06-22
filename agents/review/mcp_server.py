from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from providers.llm.base import ToolDefinition
from providers.skills.repo_cache import RepoCache
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def get_review_tools(
    repo_cache: RepoCache | None = None,
) -> list[ToolDefinition]:
    doc_tools = []
    if repo_cache:
        doc_tools = [
            ToolDefinition(
                name="list_harness_docs",
                description=(
                    "List documentation files available for a benchmark harness. "
                    "Use this to discover reference material about result formats "
                    "and interpretation."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "harness": {
                            "type": "string",
                            "description": "Harness name (e.g., 'crucible', 'zathras')",
                        },
                    },
                    "required": ["harness"],
                },
            ),
            ToolDefinition(
                name="read_harness_doc",
                description=(
                    "Read a documentation file from a benchmark harness repository. "
                    "Use this to learn about result formats, metric interpretation, "
                    "or any other harness-specific details."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "harness": {
                            "type": "string",
                            "description": "Harness name (e.g., 'crucible', 'zathras')",
                        },
                        "path": {
                            "type": "string",
                            "description": "Relative path to the doc file",
                        },
                    },
                    "required": ["harness", "path"],
                },
            ),
        ]

    return doc_tools + [
        ToolDefinition(
            name="get_review_config",
            description=(
                "Get the review/results-retrieval configuration for a benchmark "
                "harness. Returns how to find and interpret results for this "
                "harness — result storage method, directory paths, API details, "
                "and guidance notes. Call this first to learn how to access "
                "results for the harness that ran the benchmark."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness_name": {
                        "type": "string",
                        "description": "Benchmark harness name (e.g., 'crucible', 'zathras')",
                    },
                },
                "required": ["harness_name"],
            },
        ),
        ToolDefinition(
            name="retrieve_results",
            description=(
                "Retrieve benchmark result files from the controller host via SSH. "
                "Finds result files in the specified directory and returns their "
                "contents. Use the results directory from the review config or "
                "run file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {
                        "type": "string",
                        "description": "Controller hostname or IP",
                    },
                    "results_dir": {
                        "type": "string",
                        "description": "Directory path to search for results on the controller",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob pattern for result files (default: find common result files)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "SSH key path",
                    },
                },
                "required": ["controller", "results_dir"],
            },
        ),
        ToolDefinition(
            name="read_skill",
            description=(
                "Read a skill document containing lessons learned from prior "
                "benchmark runs. These may contain guidance on interpreting "
                "results for specific harnesses or benchmarks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible')",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Skill filename (e.g., 'run-file-pitfalls.md')",
                    },
                },
                "required": ["harness", "filename"],
            },
        ),
        ToolDefinition(
            name="get_run_summary",
            description=(
                "Get a structured JSON summary of a crucible benchmark run. "
                "Reads the result-summary.json from the crucible run directory. "
                "Only applicable when the harness is crucible — check "
                "get_review_config first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Benchmark run ID (UUID)"},
                    "controller": {"type": "string", "description": "Controller host"},
                    "ssh_key_path": {"type": "string", "description": "SSH key path"},
                },
                "required": ["run_id", "controller"],
            },
        ),
        ToolDefinition(
            name="cdm_api_request",
            description=(
                "Make an HTTP request to the CDM query server on the controller. "
                "Only applicable when the harness is crucible and the review "
                "config indicates cdm_api as the results method."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller host"},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path (e.g., /api/v1/run/<id>/iterations)",
                    },
                    "body": {
                        "type": "object",
                        "description": "Request body for POST requests",
                    },
                    "ssh_key_path": {"type": "string", "description": "SSH key path"},
                    "port": {
                        "type": "integer",
                        "description": "CDM server port (default: 3000)",
                    },
                },
                "required": ["controller", "method", "path"],
            },
        ),
        ToolDefinition(
            name="compare_results",
            description=(
                "Compare metrics between two benchmark runs via the CDM API. "
                "Only applicable for crucible runs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Current run ID"},
                    "baseline_id": {"type": "string", "description": "Baseline run ID"},
                    "controller": {"type": "string", "description": "Controller host"},
                    "ssh_key_path": {"type": "string", "description": "SSH key path"},
                },
                "required": ["run_id", "baseline_id", "controller"],
            },
        ),
        ToolDefinition(
            name="request_clarification",
            description="Ask the user for clarification. Pauses the ticket for human input.",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question to ask"},
                },
                "required": ["question"],
            },
        ),
        ToolDefinition(
            name="submit_review_result",
            description="Submit the performance review analysis when complete.",
            input_schema={
                "type": "object",
                "properties": {
                    "review_summary": {"type": "string", "description": "1-2 sentence summary"},
                    "verdict": {
                        "type": "string",
                        "enum": ["hypothesis_confirmed", "hypothesis_refuted", "inconclusive"],
                    },
                    "detailed_analysis": {"type": "string", "description": "Multi-paragraph markdown analysis"},
                    "key_metrics": {"type": "object", "description": "Key metric values and assessments"},
                    "recommendations": {"type": "array", "items": {"type": "string"}},
                    "follow_up_needed": {"type": "boolean"},
                    "chart_data": {
                        "type": "object",
                        "description": "Optional chart for the web dashboard. Visualize the single most informative finding from your analysis.",
                        "properties": {
                            "title": {"type": "string", "description": "Chart title, e.g. 'Throughput by Thread Count'"},
                            "type": {"type": "string", "enum": ["bar", "line", "doughnut"]},
                            "labels": {"type": "array", "items": {"type": "string"}, "description": "X-axis labels or segment names"},
                            "datasets": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "Dataset label, e.g. 'Gbps' or 'IOPS'"},
                                        "values": {"type": "array", "items": {"type": "number"}},
                                    },
                                    "required": ["label", "values"],
                                },
                            },
                        },
                        "required": ["title", "type", "labels", "datasets"],
                    },
                    "results_url": {
                        "type": "string",
                        "description": "Optional URL to a harness-specific results viewer for deeper analysis",
                    },
                },
                "required": ["review_summary", "verdict", "detailed_analysis"],
            },
        ),
    ]


def create_review_tool_handlers(
    request_clarification_fn,
    skill_provider=None,
    repo_cache: RepoCache | None = None,
) -> dict[str, Any]:

    ssh = SSHExecutor(user="root")

    async def get_review_config(harness_name: str) -> dict:
        if not skill_provider:
            return {
                "status": "error",
                "message": "No skill provider configured — cannot look up review config",
            }
        try:
            config = await skill_provider.get_all_private_config(harness_name)
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to load private config for {harness_name}: {e}",
            }
        review = config.get("review", {})
        if not review:
            execution = config.get("execution", {})
            return {
                "status": "no_review_config",
                "harness": harness_name,
                "message": (
                    f"No 'review' section found in {harness_name} private-skills config. "
                    f"Try using retrieve_results with the results directory from the "
                    f"run file or execution config."
                ),
                "results_dir_pattern": execution.get("results_dir_pattern", ""),
                "execution_keys": list(execution.keys()),
            }
        return {
            "status": "ok",
            "harness": harness_name,
            "review_config": review,
        }

    async def retrieve_results(
        controller: str,
        results_dir: str,
        file_pattern: str | None = None,
        ssh_key_path: str | None = None,
    ) -> dict:
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

        find_result = await ssh.run(
            controller, find_cmd, timeout=15, key_path=ssh_key_path,
        )
        if find_result.exit_code != 0 or not find_result.stdout.strip():
            ls_result = await ssh.run(
                controller,
                f"ls -laR {results_dir} 2>/dev/null | head -100",
                timeout=15,
                key_path=ssh_key_path,
            )
            return {
                "status": "no_files_found",
                "results_dir": results_dir,
                "pattern": file_pattern or "(default)",
                "directory_listing": ls_result.stdout[:3000] if ls_result.stdout else "",
                "message": (
                    "No matching result files found. The directory listing is "
                    "included — use it to identify the correct file paths and "
                    "call retrieve_results again with a more specific pattern."
                ),
            }

        files = find_result.stdout.strip().split("\n")
        contents = {}
        total_size = 0
        max_total = 50000

        for fpath in files:
            if total_size >= max_total:
                contents[fpath] = "(skipped — total output limit reached)"
                continue
            result = await ssh.run(
                controller,
                f"head -c 10000 '{fpath}'",
                timeout=15,
                key_path=ssh_key_path,
            )
            if result.exit_code == 0 and result.stdout:
                contents[fpath] = result.stdout
                total_size += len(result.stdout)
            else:
                contents[fpath] = f"(read error: {result.stderr[:200] if result.stderr else 'empty'})"

        return {
            "status": "ok",
            "results_dir": results_dir,
            "files_found": len(files),
            "contents": contents,
        }

    async def read_skill(harness: str, filename: str) -> dict:
        skill_path = SKILLS_DIR / harness / filename
        if not skill_path.is_file():
            available = []
            harness_dir = SKILLS_DIR / harness
            if harness_dir.is_dir():
                available = [f.name for f in harness_dir.glob("*.md")]
            return {
                "status": "not_found",
                "path": str(skill_path),
                "available": available,
            }
        return {
            "status": "ok",
            "filename": filename,
            "content": skill_path.read_text(),
        }

    async def list_harness_docs(harness: str) -> dict:
        if not repo_cache:
            return {"status": "error", "message": "No repo cache configured"}
        docs = repo_cache.list_docs(harness, subdirs=["docs", "config"])
        return {"harness": harness, "docs": docs}

    async def read_harness_doc(harness: str, path: str) -> dict:
        if not repo_cache:
            return {"status": "error", "message": "No repo cache configured"}
        content = repo_cache.read_file(harness, path)
        if content is None:
            return {"status": "not_found", "harness": harness, "path": path}
        return {"status": "ok", "path": path, "content": content[:15000]}

    async def get_run_summary(
        run_id: str, controller: str, ssh_key_path: str | None = None,
    ) -> dict:
        find_result = await ssh.run(
            controller,
            f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
            timeout=15,
            key_path=ssh_key_path,
        )
        run_dir = find_result.stdout.strip() if find_result.exit_code == 0 else ""
        if not run_dir:
            return {
                "run_id": run_id,
                "status": "not_found",
                "message": f"No run directory found matching {run_id}",
            }

        summary_path = f"{run_dir}/run/result-summary.json"
        result = await ssh.run(
            controller, f"cat {summary_path}", timeout=30, key_path=ssh_key_path,
        )
        if result.exit_code != 0:
            return {
                "run_id": run_id,
                "status": "error",
                "run_dir": run_dir,
                "message": f"{summary_path} not found — run may still be indexing",
                "stderr": result.stderr[:500] if result.stderr else "",
            }

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "run_id": run_id,
                "status": "error",
                "message": "result-summary.json exists but is not valid JSON",
                "raw": result.stdout[:2000],
            }

    async def cdm_api_request(
        controller: str,
        method: str,
        path: str,
        body: dict | None = None,
        ssh_key_path: str | None = None,
        port: int = 3000,
    ) -> dict:
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

        result = await ssh.run(controller, cmd, timeout=60, key_path=ssh_key_path)
        if result.exit_code != 0:
            return {
                "status": "error",
                "method": method,
                "path": path,
                "exit_code": result.exit_code,
                "error": result.stderr or "",
            }

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "method": method,
                "path": path,
                "raw_output": result.stdout or "",
                "error": "Response is not valid JSON",
            }

    async def compare_results(
        run_id: str, baseline_id: str, controller: str,
        ssh_key_path: str | None = None,
    ) -> dict:
        current = await cdm_api_request(
            controller, "POST", "/api/v1/iterations/metric-values",
            body={"runIds": [run_id]},
            ssh_key_path=ssh_key_path,
        )
        baseline = await cdm_api_request(
            controller, "POST", "/api/v1/iterations/metric-values",
            body={"runIds": [baseline_id]},
            ssh_key_path=ssh_key_path,
        )
        return {
            "current_run": {"run_id": run_id, "data": current},
            "baseline_run": {"run_id": baseline_id, "data": baseline},
        }

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    handlers = {
        "get_review_config": get_review_config,
        "retrieve_results": retrieve_results,
        "read_skill": read_skill,
        "get_run_summary": get_run_summary,
        "cdm_api_request": cdm_api_request,
        "compare_results": compare_results,
        "request_clarification": request_clarification,
    }

    if repo_cache:
        handlers["list_harness_docs"] = list_harness_docs
        handlers["read_harness_doc"] = read_harness_doc

    return handlers
