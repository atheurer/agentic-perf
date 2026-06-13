from __future__ import annotations

import json
import logging
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)


def get_review_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_run_summary",
            description=(
                "Get a structured JSON summary of a benchmark run. Reads the "
                "result-summary.json that crucible writes to the run directory "
                "at the end of every run. Returns iterations, params, primary "
                "metrics, sample values, tags, and available metric sources. "
                "Pass the controller from ssh_hardware_ips."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Benchmark run ID (UUID)"},
                    "controller": {"type": "string", "description": "Controller host IP (from ssh_hardware_ips)"},
                    "ssh_key_path": {"type": "string", "description": "SSH key path (from ticket fields)"},
                },
                "required": ["run_id", "controller"],
            },
        ),
        ToolDefinition(
            name="cdm_api_request",
            description=(
                "Make an HTTP request to the CDM query server running on the "
                "controller host (port 3000). Use this to query benchmark "
                "metrics, iteration details, time-series data, and breakouts. "
                "The CDM data model is: run -> iterations -> samples -> periods -> metrics. "
                "Key endpoints:\n"
                "  GET  /api/v1/runs?run=<id> - find runs\n"
                "  GET  /api/v1/run/<id>/tags - run tags\n"
                "  GET  /api/v1/run/<id>/benchmark - benchmark name\n"
                "  GET  /api/v1/run/<id>/iterations - iteration IDs\n"
                "  POST /api/v1/run/<id>/iterations/params - params per iteration (body: {iterations: [...]})\n"
                "  POST /api/v1/run/<id>/iterations/primary-metric - primary metric per iteration\n"
                "  POST /api/v1/run/<id>/iterations/samples - sample IDs per iteration\n"
                "  POST /api/v1/run/<id>/samples/statuses - pass/fail per sample (body: {sampleIds: [...]})\n"
                "  GET  /api/v1/run/<id>/metric-sources - available metric sources (fio, mpstat, etc.)\n"
                "  POST /api/v1/run/<id>/metric-types - metric types per source (body: {sources: [...]})\n"
                "  POST /api/v1/metric-data - time-series metric data (body: {run, source, type, begin, end, resolution, breakout})\n"
                "  POST /api/v1/iterations/metric-values - bulk metric values for runs (body: {runIds: [...]})"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller host IP (from ssh_hardware_ips)"},
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
                "Compare metrics between two benchmark runs. Fetches "
                "iteration metric values for both runs via the CDM API "
                "and returns them for analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Current run ID"},
                    "baseline_id": {"type": "string", "description": "Baseline run ID"},
                    "controller": {"type": "string", "description": "Controller host IP"},
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
                },
                "required": ["review_summary", "verdict", "detailed_analysis"],
            },
        ),
    ]


def create_review_tool_handlers(
    request_clarification_fn,
) -> dict[str, Any]:

    ssh = SSHExecutor(user="root")

    async def get_run_summary(
        run_id: str, controller: str, ssh_key_path: str | None = None,
    ) -> dict:
        # Find the run directory by UUID
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
                "error": result.stderr[-500:] if result.stderr else "",
            }

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "method": method,
                "path": path,
                "raw_output": result.stdout[-2000:] if result.stdout else "",
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

    return {
        "get_run_summary": get_run_summary,
        "cdm_api_request": cdm_api_request,
        "compare_results": compare_results,
        "request_clarification": request_clarification,
    }
