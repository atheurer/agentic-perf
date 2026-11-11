"""FastMCP server for review agent tools.

Exposes result-retrieval, skill/doc, and config tools over stdio.
SSH credentials are resolved from the ticket via _ensure_init(),
never passed as tool parameters — this is a security improvement
over the original mcp_server.py which exposed ssh_key_path to the LLM.

Run directly:  python agents/review/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.crucible_mcp import CrucibleMCPAdapter
from agents.mcp_audit import create_ticket_mcp
from agents.server_utils import (
    build_crucible_context_gateway,
    build_repo_cache,
    build_skill_provider_async,
    build_ssh_from_ticket,
    controller_context_gateway,
    crucible_context_gateway,
    read_skill_documents,
    ticket_controller_host,
    ticket_state_headers,
)
from agents.skill_gateway import (
    SKILL_GATEWAY_TOOL_DESCRIPTION,
    organization_manages_harness,
    skill_config_view,
    skill_context_gateway,
)
from providers.execution import AuditedAsyncHTTPClient

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("review-agent")


SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Module-level globals — lazily initialized by _ensure_init()
_initialized = False
_cdm_initialized = False
_cdm_init_lock = asyncio.Lock()
_ssh = None
_skill_provider = None
_crucible_context = None
_repo_cache = None
_ticket: dict[str, Any] = {}
_crucible_mcp: CrucibleMCPAdapter | None = None


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global \
        _initialized, \
        _ssh, \
        _skill_provider, \
        _crucible_context, \
        _repo_cache, \
        _ticket, \
        _crucible_mcp
    if _initialized:
        return
    async with _cdm_init_lock:
        if _initialized:
            return
        ssh, ticket = await build_ssh_from_ticket()
        provider = await build_skill_provider_async(skill_phase="review")
        context = build_crucible_context_gateway(catalog_only=False)
        try:
            repo_cache = await build_repo_cache()
        except Exception:
            repo_cache = None
        _ssh, _ticket, _skill_provider, _crucible_context, _repo_cache = (
            ssh,
            ticket,
            provider,
            context,
            repo_cache,
        )
        controller = ticket_controller_host(ticket)
        ticket_id = str(ticket.get("id") or os.environ.get("TICKET_ID", ""))
        if controller and ticket_id:
            _crucible_mcp = CrucibleMCPAdapter.from_config(
                controller=controller,
                ticket_id=ticket_id,
                agent_id="review-agent",
            )
        _initialized = True


async def _ensure_cdm_init():
    """Load ticket and review configuration without resolving SSH credentials."""
    global _cdm_initialized, _ticket, _skill_provider, _crucible_mcp
    if _initialized or _cdm_initialized:
        return
    async with _cdm_init_lock:
        if _initialized or _cdm_initialized:
            return
        ticket_id = os.environ.get("TICKET_ID", "")
        if ticket_id:
            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            async with AuditedAsyncHTTPClient(
                timeout=15.0, headers=ticket_state_headers()
            ) as client:
                response = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
                response.raise_for_status()
                ticket = response.json()
        else:
            ticket = {}
        provider = await build_skill_provider_async(skill_phase="review")
        _ticket = ticket
        _skill_provider = provider
        controller = ticket_controller_host(ticket)
        ticket_id = str(ticket.get("id") or os.environ.get("TICKET_ID", ""))
        if controller and ticket_id:
            _crucible_mcp = CrucibleMCPAdapter.from_config(
                controller=controller,
                ticket_id=ticket_id,
                agent_id="review-agent",
            )
        _cdm_initialized = True


# ---------------------------------------------------------------------------
# Skill / Doc tools
# ---------------------------------------------------------------------------


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
    await _ensure_init()
    return await skill_context_gateway(
        _skill_provider,
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="review-agent",
        phase="review",
        ssh=_ssh,
        controller_host=ticket_controller_host(_ticket),
        mcp_adapter=_crucible_mcp,
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
        repo_cache=_repo_cache,
    )


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read one or more skill documents in one call. Each entry in docs must be a dict with 'harness' and 'filename' (e.g. [{'harness': 'zathras', 'filename': 'local-config-guide.md'}]). These may contain guidance on interpreting results for specific harnesses or benchmarks."""
    await _ensure_init()
    results = []
    for doc in docs:
        harness = doc.get("harness") or doc.get("category", "")
        filename = doc.get("filename") or doc.get("name", "")
        if isinstance(harness, str) and harness.strip().lower() == "crucible":
            results.append(
                {
                    "found": False,
                    "harness": harness,
                    "filename": filename,
                    "message": (
                        "Use get_skill_context(subject='harness/crucible', "
                        "operation='bootstrap')."
                    ),
                }
            )
        elif harness == "kube-burner":
            results.append(
                {
                    "found": False,
                    "harness": harness,
                    "filename": filename,
                    "message": (
                        "Use get_skill_context(subject='harness/kube-burner', "
                        "operation='bootstrap')."
                    ),
                }
            )
        else:
            results.extend(read_skill_documents(SKILLS_DIR, [doc]))
    return json.dumps(results)


async def _get_crucible_benchmark_context_tool(
    operation: str = "bootstrap",
    path: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Use generic context primitives for the designated Crucible controller."""
    await _ensure_init()
    if operation not in {"bootstrap", "read", "search"}:
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "unsupported_operation",
                "guidance": "Use bootstrap, read, or search.",
            }
        )
    return await controller_context_gateway(
        ssh=_ssh,
        controller_host=ticket_controller_host(_ticket),
        mcp_adapter=_crucible_mcp,
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="review-agent",
        phase="review",
        operation=operation,
        path=path,
        query=query,
        benchmark="",
        include_alternates=False,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
    )


async def _legacy_get_crucible_benchmark_context(
    benchmark: str = "",
    operation: str = "list",
    namespace: str = "all",
    path: str = "",
    subject_area: str | list[str] = "all",
    include_alternates: bool = False,
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Compatibility implementation for pre-gateway internal callers."""
    await _ensure_init()
    if operation in {"bootstrap", "list", "read", "search"}:
        gateway_operation = "manifest" if operation == "list" else operation
        return await controller_context_gateway(
            ssh=_ssh,
            controller_host=ticket_controller_host(_ticket),
            mcp_adapter=_crucible_mcp,
            ticket_id=os.environ.get("TICKET_ID", ""),
            agent_name="review-agent",
            phase="review",
            benchmark=benchmark,
            operation=gateway_operation,
            path=path,
            query=query,
            include_alternates=include_alternates,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )
    return await crucible_context_gateway(
        _crucible_context,
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="review-agent",
        phase="review",
        benchmark=benchmark,
        operation=operation,
        namespace=namespace,
        path=path,
        subject_area=subject_area,
        include_alternates=include_alternates,
        query=query,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
    )


async def get_crucible_benchmark_context(*args, **kwargs) -> str:
    """Compatibility wrapper for direct in-process callers only."""
    return await _legacy_get_crucible_benchmark_context(*args, **kwargs)


@mcp.tool()
async def list_harness_docs(harness: str) -> str:
    """List documentation files available for a benchmark harness. Use this to discover reference material about result formats and interpretation."""
    await _ensure_init()
    if isinstance(harness, str) and harness.strip().lower() == "crucible":
        return json.dumps(
            {
                "found": False,
                "message": "Use get_skill_context(subject='harness/crucible', operation='bootstrap').",
            }
        )
    if harness == "kube-burner":
        return json.dumps(
            {
                "harness": harness,
                "docs": [],
                "message": "Use get_skill_context(subject='harness/kube-burner', operation='bootstrap').",
            }
        )
    if not _repo_cache:
        return json.dumps({"status": "error", "message": "No repo cache configured"})
    docs = _repo_cache.list_docs(harness, subdirs=["docs", "config"])
    return json.dumps({"harness": harness, "docs": docs})


@mcp.tool()
async def read_harness_doc(harness: str, doc_path: str) -> str:
    """Read a documentation file from a benchmark harness repository (e.g. harness='crucible', doc_path='docs/how-run-files-work.md'). Use this to learn about result formats, metric interpretation, or any other harness-specific details."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"status": "error", "message": "No repo cache configured"})
    if not harness and "/" in doc_path:
        harness, doc_path = doc_path.strip().lstrip("/").split("/", 1)
    if isinstance(harness, str) and harness.strip().lower() == "crucible":
        return json.dumps(
            {
                "found": False,
                "message": "Use get_skill_context(subject='harness/crucible', operation='bootstrap').",
            }
        )
    if harness == "kube-burner":
        return json.dumps(
            {
                "status": "gateway_required",
                "message": "Use get_skill_context(subject='harness/kube-burner', operation='bootstrap').",
            }
        )
    content = _repo_cache.read_file(harness, doc_path)
    if content is None:
        return json.dumps({"status": "not_found", "harness": harness, "path": doc_path})
    return json.dumps({"status": "ok", "path": doc_path, "content": content[:15000]})


# ---------------------------------------------------------------------------
# MCP-backed result and indexed-metric tools
# ---------------------------------------------------------------------------


def _decode_mcp_result(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Crucible MCP returned an invalid response") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Crucible MCP returned an invalid response")
    return decoded


def _controller_error(reason: str, message: str) -> str:
    return json.dumps({"status": "error", "reason": reason, "message": message})


def _review_adapter(controller: str) -> tuple[Any | None, str | None]:
    assigned_controller = ticket_controller_host(_ticket)
    if not assigned_controller or controller != assigned_controller:
        return None, _controller_error(
            "controller_mismatch",
            "The controller must match the ticket's assigned Crucible controller.",
        )
    if _crucible_mcp is None:
        return None, _controller_error(
            "unsupported_controller",
            "The assigned Crucible controller does not support MCP contract v3.",
        )
    return _crucible_mcp, None


def _unknown_mcp_identifier(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in ("unknown mcp job", "jobnotfound", "not found", "no such job")
    )


async def _find_local_run_path(
    adapter: Any,
    run_id: str,
) -> tuple[str | None, str | None]:
    offset = 0
    for _ in range(10):
        raw = await adapter.call_tool(
            "list_local_runs", {"limit": 1000, "offset": offset}
        )
        payload = _decode_mcp_result(raw)
        for run in payload.get("runs", []):
            if not isinstance(run, dict) or run.get("run_id") != run_id:
                continue
            run_path = run.get("path")
            if isinstance(run_path, str) and run_path:
                return run_path, None
        next_offset = payload.get("next_offset")
        complete = payload.get("complete") is True
        if complete or not isinstance(next_offset, int) or next_offset <= offset:
            break
        offset = next_offset
    return None, (
        "The historical run was not found within bounded local-run pagination."
    )


async def _run_artifact_context(
    controller: str,
    run_id: str,
    *,
    full_listing: bool,
    listing_offset: int = 0,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    adapter, error = _review_adapter(controller)
    if adapter is None or error is not None:
        return None, None, error
    try:
        raw = await adapter.call_tool(
            "list_run_artifacts",
            {
                "mcp_job_id": run_id,
                "limit": 1000 if full_listing else 1,
                "offset": listing_offset,
            },
        )
        return {"mcp_job_id": run_id}, _decode_mcp_result(raw), None
    except Exception as exc:
        if not _unknown_mcp_identifier(exc):
            return (
                None,
                None,
                _controller_error(
                    "mcp_transport_error",
                    "The Crucible MCP artifact request failed.",
                ),
            )
    run_path, lookup_error = await _find_local_run_path(adapter, run_id)
    if run_path is None:
        return (
            None,
            None,
            _controller_error(
                "run_not_found", lookup_error or "The run was not found."
            ),
        )
    try:
        raw = await adapter.call_tool(
            "list_run_artifacts",
            {
                "run_path": run_path,
                "limit": 1000 if full_listing else 1,
                "offset": listing_offset,
            },
        )
        return {"run_path": run_path}, _decode_mcp_result(raw), None
    except Exception:
        return (
            None,
            None,
            _controller_error(
                "mcp_transport_error",
                "The Crucible MCP artifact request failed.",
            ),
        )


@mcp.tool()
async def read_run_results(
    controller: str,
    run_id: str,
    file_path: str = "",
    max_bytes: int = 4000,
    offset: int = 0,
    listing_offset: int = 0,
) -> str:
    """List or read bounded, approved artifacts for a Crucible MCP job or historical run."""
    await _ensure_cdm_init()
    if type(listing_offset) is not int or listing_offset < 0:
        return json.dumps(
            {
                "status": "error",
                "reason": "invalid_listing_offset",
                "message": "listing_offset must be a nonnegative integer.",
            }
        )
    target, listing, error = await _run_artifact_context(
        controller,
        run_id,
        full_listing=not file_path,
        listing_offset=listing_offset if not file_path else 0,
    )
    if target is None or listing is None or error is not None:
        return error or _controller_error("run_not_found", "The run was not found.")

    run_path = target.get("run_path") or listing.get("run_path")
    if not file_path:
        files = []
        for artifact in listing.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            artifact_path = artifact.get("artifact_path")
            if not isinstance(artifact_path, str):
                continue
            files.append(
                {
                    "path": artifact_path,
                    "size_bytes": artifact.get("size"),
                    "retrievable": artifact.get("retrievable") is True,
                }
            )
        return json.dumps(
            {
                "status": "ok",
                "run_id": run_id,
                "run_path": run_path,
                "files": files,
                "complete": listing.get("complete") is True,
                "listing_offset": listing_offset,
                "next_offset": listing.get("next_offset"),
                "message": (
                    "Use a returned relative artifact_path. Compressed artifacts "
                    "and artifacts over the MCP bound are reported as unsupported."
                ),
            }
        )

    if file_path.endswith(".xz"):
        return json.dumps(
            {
                "status": "unsupported",
                "run_id": run_id,
                "file_path": file_path,
                "reason": "compressed_artifact_not_supported",
                "message": "MCP artifact reads do not decompress .xz artifacts.",
            }
        )
    if type(offset) is not int or offset < 0:
        return json.dumps(
            {
                "status": "error",
                "reason": "invalid_offset",
                "message": "offset must be a nonnegative integer.",
            }
        )
    limit = min(max(max_bytes, 100), 50_000)
    adapter, adapter_error = _review_adapter(controller)
    if adapter is None or adapter_error is not None:
        return adapter_error or _controller_error(
            "unsupported_controller", "Crucible MCP is unavailable."
        )
    try:
        raw = await adapter.call_tool(
            "get_run_artifact",
            {
                **target,
                "artifact_path": file_path,
                "offset": offset,
                "limit": limit,
            },
        )
        artifact = _decode_mcp_result(raw)
    except Exception:
        return json.dumps(
            {
                "status": "unsupported",
                "run_id": run_id,
                "file_path": file_path,
                "reason": "artifact_not_retrievable",
                "message": (
                    "The artifact is not retrievable through the bounded MCP "
                    "artifact contract."
                ),
            }
        )
    text = artifact.get("text")
    if not isinstance(text, str):
        return json.dumps(
            {
                "status": "error",
                "run_id": run_id,
                "file_path": file_path,
                "reason": "invalid_artifact_response",
                "message": "Crucible returned an invalid artifact response.",
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "run_id": run_id,
            "file_path": file_path,
            "resolved_path": artifact.get("artifact_path", file_path),
            "run_path": artifact.get("run_path", run_path),
            "bytes_read": len(text.encode("utf-8")),
            "file_size": artifact.get("size"),
            "offset": artifact.get("offset", offset),
            "next_offset": artifact.get("next_offset"),
            "truncated": artifact.get("complete") is not True,
            "content": text,
        }
    )


@mcp.tool()
async def get_run_summary(
    controller: str,
    run_id: str,
    harness: str = "crucible",
) -> str:
    """Get a completed Crucible summary by MCP job ID or verified historical run ID."""
    await _ensure_cdm_init()
    if harness != "crucible":
        return _controller_error(
            "unsupported_harness", "Run summaries are only available for Crucible."
        )
    adapter, error = _review_adapter(controller)
    if adapter is None or error is not None:
        return error
    try:
        raw = await adapter.call_tool("get_run_summary", {"mcp_job_id": run_id})
        result = _decode_mcp_result(raw)
        return json.dumps({**result, "run_id": run_id, "identifier_type": "mcp_job"})
    except Exception as exc:
        if not _unknown_mcp_identifier(exc):
            return _controller_error(
                "mcp_transport_error",
                "The Crucible MCP summary request failed.",
            )

    run_path, lookup_error = await _find_local_run_path(adapter, run_id)
    if run_path is None:
        return _controller_error(
            "run_not_found", lookup_error or "The run was not found."
        )
    try:
        raw = await adapter.call_tool("get_local_run_summary", {"run_path": run_path})
        result = _decode_mcp_result(raw)
    except Exception:
        return _controller_error(
            "mcp_transport_error",
            "The Crucible MCP historical summary request failed.",
        )
    return json.dumps({**result, "run_id": run_id, "identifier_type": "historical_run"})


@mcp.tool()
async def list_cdm_periods(
    controller: str,
    run_id: str,
) -> str:
    """Discover indexed primary periods for a historical Crucible CDM run."""
    await _ensure_cdm_init()
    adapter, error = _review_adapter(controller)
    if adapter is None or error is not None:
        return error
    try:
        raw = await adapter.call_tool("list_indexed_periods", {"run": run_id})
        return json.dumps(_decode_mcp_result(raw))
    except Exception:
        return _controller_error(
            "mcp_transport_error", "The Crucible period discovery request failed."
        )


@mcp.tool()
async def get_cdm_metric(
    controller: str,
    run_id: str,
    source: str,
    metric_type: str,
    period: str = "",
    begin: int | None = None,
    end: int | None = None,
    aggregation: str | None = None,
    breakout: list[str] | None = None,
    filter: str = "",
    resolution: int = 1,
) -> str:
    """Query one structured CDM metric after period discovery."""
    await _ensure_cdm_init()
    adapter, error = _review_adapter(controller)
    if adapter is None or error is not None:
        return error
    if not period and (begin is None or end is None):
        return _controller_error(
            "period_required",
            "Provide a discovered period or both begin and end.",
        )
    if aggregation is not None and aggregation not in {"sum", "avg", "max", "min"}:
        return _controller_error(
            "invalid_aggregation",
            "aggregation must be sum, avg, max, or min.",
        )
    arguments: dict[str, Any] = {
        "run": run_id,
        "source": source,
        "type": metric_type,
        "resolution": resolution,
    }
    if period:
        arguments["period"] = period
    if begin is not None:
        arguments["begin"] = begin
    if end is not None:
        arguments["end"] = end
    if aggregation is not None:
        arguments["aggregation"] = aggregation
    if breakout:
        arguments["breakout"] = breakout
    if filter:
        arguments["filter"] = filter
    try:
        raw = await adapter.call_tool("get_indexed_metric", arguments)
        return json.dumps(_decode_mcp_result(raw))
    except Exception:
        return _controller_error(
            "mcp_transport_error", "The structured CDM metric query failed."
        )


def _metric_aggregates(
    result: dict[str, Any], aggregation: str
) -> tuple[dict[str, Any], int]:
    values = result.get("values")
    if not isinstance(values, dict):
        return {}, 0
    aggregates: dict[str, Any] = {}
    non_numeric = 0
    for breakout_key, samples in values.items():
        if not isinstance(samples, list):
            continue
        numeric = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            value = sample.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric.append(float(value))
            else:
                non_numeric += 1
        if not numeric:
            continue
        if aggregation == "sum":
            aggregate = sum(numeric)
        elif aggregation == "min":
            aggregate = min(numeric)
        elif aggregation == "max":
            aggregate = max(numeric)
        else:
            aggregate = sum(numeric) / len(numeric)
        aggregates[str(breakout_key)] = {
            "aggregation": aggregation,
            "value": aggregate,
            "samples": len(numeric),
        }
    return aggregates, non_numeric


@mcp.tool()
async def compare_results(
    controller: str,
    run_ids: list[str],
    metric_name: str,
    period: str = "",
    periods: dict[str, str] | None = None,
    aggregation: str = "avg",
    breakout: list[str] | None = None,
    filter: str = "",
) -> str:
    """Compare a period-scoped CDM metric across two or more historical runs."""
    await _ensure_cdm_init()
    adapter, error = _review_adapter(controller)
    if adapter is None or error is not None:
        return error
    if len(run_ids) < 2:
        return _controller_error(
            "invalid_comparison", "At least two run IDs are required."
        )
    if aggregation not in {"sum", "avg", "max", "min"}:
        return _controller_error(
            "invalid_aggregation",
            "aggregation must be sum, avg, max, or min.",
        )
    separator = "::" if "::" in metric_name else "/" if "/" in metric_name else ""
    if not separator:
        return _controller_error(
            "invalid_metric_name",
            "metric_name must use source::type or source/type.",
        )
    source, metric_type = metric_name.split(separator, 1)
    if not source or not metric_type:
        return _controller_error(
            "invalid_metric_name",
            "metric_name must include both source and type.",
        )

    period_mapping = periods or {}
    if not isinstance(period_mapping, dict):
        return _controller_error(
            "invalid_period_mapping",
            "periods must map each run ID to a nonempty period ID.",
        )
    unknown_run_ids = [run_id for run_id in period_mapping if run_id not in run_ids]
    if unknown_run_ids:
        return json.dumps(
            {
                "status": "invalid_period_mapping",
                "run_ids": run_ids,
                "metric_name": metric_name,
                "unknown_run_ids": unknown_run_ids,
                "message": "The period mapping contains unknown run IDs.",
            }
        )
    invalid_period_ids = [
        run_id
        for run_id, mapped_period in period_mapping.items()
        if not isinstance(mapped_period, str) or not mapped_period
    ]
    if invalid_period_ids:
        return json.dumps(
            {
                "status": "invalid_period_mapping",
                "run_ids": run_ids,
                "metric_name": metric_name,
                "run_ids_with_invalid_periods": invalid_period_ids,
                "message": "Each period mapping value must be a nonempty string.",
            }
        )

    selected_period_by_run: dict[str, str] = {}
    discovered_periods: dict[str, list[str]] = {}
    for run_id in run_ids:
        try:
            raw = await adapter.call_tool("list_indexed_periods", {"run": run_id})
            period_result = _decode_mcp_result(raw)
        except Exception:
            return _controller_error(
                "period_discovery_failed",
                f"Period discovery failed for run {run_id}.",
            )
        periods = []
        for item in period_result.get("periods", []):
            if isinstance(item, dict) and isinstance(
                item.get("primary_period_id"), str
            ):
                periods.append(item["primary_period_id"])
        discovered_periods[run_id] = periods
        unique_periods = list(dict.fromkeys(periods))

        selected_period = period_mapping.get(run_id)
        if selected_period is None and period:
            selected_period = period
        if selected_period is None and len(unique_periods) == 1:
            selected_period = unique_periods[0]

        if selected_period and selected_period not in unique_periods:
            return json.dumps(
                {
                    "status": "invalid_period",
                    "run_ids": run_ids,
                    "metric_name": metric_name,
                    "run_id": run_id,
                    "requested_period": selected_period,
                    "discovered_periods": unique_periods,
                    "message": "The requested period was not discovered for this run.",
                }
            )
        if selected_period:
            selected_period_by_run[run_id] = selected_period

    ambiguous = {
        run_id: periods
        for run_id, periods in discovered_periods.items()
        if run_id not in selected_period_by_run
    }
    if ambiguous:
        return json.dumps(
            {
                "status": "ambiguous_period",
                "run_ids": run_ids,
                "metric_name": metric_name,
                "periods": ambiguous,
                "message": (
                    "Period discovery returned zero or multiple periods; select "
                    "an explicit period instead of choosing arbitrarily."
                ),
            }
        )

    results: dict[str, Any] = {}
    for run_id in run_ids:
        arguments: dict[str, Any] = {
            "run": run_id,
            "source": source,
            "type": metric_type,
            "period": selected_period_by_run[run_id],
            "aggregation": aggregation,
        }
        if breakout:
            arguments["breakout"] = breakout
        if filter:
            arguments["filter"] = filter
        try:
            raw = await adapter.call_tool("get_indexed_metric", arguments)
            metric_result = _decode_mcp_result(raw)
        except Exception:
            return _controller_error(
                "mcp_transport_error",
                f"The structured CDM metric query failed for run {run_id}.",
            )
        aggregates, non_numeric = _metric_aggregates(metric_result, aggregation)
        results[run_id] = {
            "period": selected_period_by_run[run_id],
            "metric": metric_result,
            "aggregates": aggregates,
            "non_numeric_samples": non_numeric,
        }
    return json.dumps(
        {
            "status": "ok",
            "run_ids": run_ids,
            "metric_name": metric_name,
            "aggregation": aggregation,
            "periods": selected_period_by_run,
            "discovered_periods": discovered_periods,
            "results": results,
        }
    )


# ---------------------------------------------------------------------------
# Config tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_review_config(harness_name: str) -> str:
    """Get review/results-retrieval settings for the benchmark harness. Crucible returns approved method, port, and result-location fields; read interpretation guidance through get_skill_context. Check ticket artifacts before retrieving remote results."""
    await _ensure_cdm_init()

    if harness_name == "crucible" or organization_manages_harness(
        _skill_provider, harness_name
    ):
        try:
            review = await skill_config_view(
                _skill_provider, harness_name, "review", "review"
            )
        except Exception:
            return json.dumps(
                {"status": "error", "reason": "configuration_view_unavailable"}
            )
        return json.dumps(
            {
                "status": "ok" if review else "no_review_config",
                "harness": harness_name,
                "review_config": review,
            }
        )
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
                    f"Check output_dir/output_dirs on the ticket for local artifacts "
                    f"(use list_benchmark_artifacts + read_benchmark_artifact). "
                    f"For Crucible, use get_run_summary and read_run_results "
                    f"through MCP."
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


async def get_registered_tools():
    """Introspect this server's registered @mcp.tool() functions."""
    from providers.llm.base import ToolDefinition

    tools = await mcp.list_tools()
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or "",
            input_schema=t.parameters,
        )
        for t in tools
    ]


if __name__ == "__main__":
    mcp.run()
