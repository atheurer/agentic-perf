"""FastMCP server for introspection agent tools.

Exposes read-only observation tools over stdio. The introspection
agent uses these to watch a ticket's event stream, check token
usage, and detect anomalies — all without modifying ticket state.

Run directly:  python agents/introspection/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import httpx
from fastmcp import FastMCP

from paths import LOG_DIR as DEFAULT_LOG_DIR

mcp = FastMCP("introspection")


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _state_store_url() -> str:
    return os.environ.get("STATE_STORE_URL", "http://localhost:8080")


def _read_events(
    ticket_id: str,
    since: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read events from the JSONL file for a ticket."""
    path = DEFAULT_LOG_DIR / f"{ticket_id}.jsonl"
    if not path.exists():
        return []
    results: list[dict[str, Any]] = []
    line_num = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            line_num += 1
            evt["seq"] = line_num
            if line_num > since:
                results.append(evt)
                if len(results) >= limit:
                    break
    return results


def _truncate_event(evt: dict[str, Any]) -> dict[str, Any]:
    """Trim large payloads from events for token efficiency."""
    trimmed: dict[str, Any] = {
        "seq": evt.get("seq"),
        "timestamp": evt.get("timestamp"),
        "agent": evt.get("agent"),
        "event_type": evt.get("event_type"),
    }
    data = evt.get("data", {})
    etype = evt.get("event_type")

    if etype == "llm_response":
        trimmed["data"] = {
            "iteration": data.get("iteration"),
            "stop_reason": data.get("stop_reason"),
            "tool_calls": data.get("tool_calls", []),
            "text_length": data.get("text_length", 0),
            "text": (data.get("text") or "")[:500],
        }
    elif etype == "tool_called":
        input_data = data.get("input", {})
        input_str = json.dumps(input_data, default=str)
        trimmed["data"] = {
            "tool": data.get("tool"),
            "input": (
                input_data
                if len(input_str) <= 500
                else {"_truncated": input_str[:500] + "..."}
            ),
        }
    elif etype == "tool_result":
        trimmed["data"] = {
            "tool": data.get("tool"),
            "is_error": data.get("is_error"),
            "content_length": data.get("content_length", 0),
            "content": (data.get("content") or "")[:500],
        }
    else:
        trimmed["data"] = data

    return trimmed


def _detect_anomalies_from_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Analyze events for anomalous patterns."""
    anomalies: list[dict[str, Any]] = []

    # Track consecutive tool errors by tool name
    error_counts: dict[str, list[int]] = {}
    for evt in events:
        if evt.get("event_type") == "tool_result" and evt.get("data", {}).get(
            "is_error"
        ):
            tool = evt.get("data", {}).get("tool", "unknown")
            error_counts.setdefault(tool, []).append(evt.get("seq", 0))

    for tool, seqs in error_counts.items():
        if len(seqs) >= 3:
            anomalies.append(
                {
                    "type": "repeated_error",
                    "severity": "high" if len(seqs) >= 5 else "medium",
                    "description": (f"Tool '{tool}' failed {len(seqs)} times"),
                    "seq_range": [seqs[0], seqs[-1]],
                }
            )

    # Detect tool call loops (same tool + same input consecutively)
    prev_call: dict[str, Any] | None = None
    loop_count = 1
    loop_start_seq = 0
    for evt in events:
        if evt.get("event_type") != "tool_called":
            continue
        data = evt.get("data", {})
        current = {
            "tool": data.get("tool"),
            "input": json.dumps(data.get("input", {}), sort_keys=True),
        }
        if prev_call and current == prev_call:
            loop_count += 1
        else:
            if loop_count >= 3 and prev_call:
                anomalies.append(
                    {
                        "type": "retry_loop",
                        "severity": "high" if loop_count >= 5 else "medium",
                        "description": (
                            f"Tool '{prev_call['tool']}' called "
                            f"{loop_count} times with identical input"
                        ),
                        "seq_range": [loop_start_seq, evt.get("seq", 0) - 1],
                    }
                )
            loop_count = 1
            loop_start_seq = evt.get("seq", 0)
        prev_call = current

    if loop_count >= 3 and prev_call:
        anomalies.append(
            {
                "type": "retry_loop",
                "severity": "high" if loop_count >= 5 else "medium",
                "description": (
                    f"Tool '{prev_call['tool']}' called "
                    f"{loop_count} times with identical input"
                ),
                "seq_range": [loop_start_seq, events[-1].get("seq", 0)],
            }
        )

    # Detect max_iterations hits
    for evt in events:
        if (
            evt.get("event_type") == "agent_error"
            and evt.get("data", {}).get("reason") == "max_iterations"
        ):
            anomalies.append(
                {
                    "type": "excessive_iterations",
                    "severity": "high",
                    "description": (
                        f"Agent '{evt.get('agent', 'unknown')}' hit max iteration limit"
                    ),
                    "seq_range": [evt.get("seq", 0)],
                }
            )

    return anomalies


@mcp.tool()
async def get_ticket_events(
    ticket_id: str,
    since: int = 0,
    limit: int = 100,
) -> str:
    """Fetch recent events from a ticket's event stream.

    Returns truncated events for token efficiency. Use 'since'
    to poll incrementally for new events.
    """
    events = _read_events(ticket_id, since=since, limit=limit)
    trimmed = [_truncate_event(e) for e in events]
    return json.dumps(trimmed, indent=2, default=str)


@mcp.tool()
async def get_ticket_status(ticket_id: str) -> str:
    """Get the current status and metadata of a ticket."""
    url = f"{_state_store_url()}/api/v1/tickets/{ticket_id}"
    async with httpx.AsyncClient(
        timeout=10.0,
        headers=_auth_headers(),
    ) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return json.dumps({"error": f"Ticket {ticket_id} not found"})
        ticket = r.json()

    # Return a trimmed view — don't send the full ticket
    # (which may contain large custom_fields) to the LLM.
    cf = ticket.get("custom_fields", {})
    return json.dumps(
        {
            "id": ticket.get("id"),
            "summary": ticket.get("summary"),
            "status": ticket.get("status"),
            "created_at": ticket.get("created_at"),
            "updated_at": ticket.get("updated_at"),
            "benchmark_suite": cf.get("benchmark_suite"),
            "harness_name": cf.get("harness_name"),
            "resource_provider": cf.get("resource_provider"),
            "hypothesis": cf.get("hypothesis"),
            "comment_count": len(ticket.get("comments", [])),
        },
        indent=2,
        default=str,
    )


@mcp.tool()
async def get_token_usage(ticket_id: str) -> str:
    """Get cumulative LLM token usage for a ticket.

    Reads usage data from the event stream (llm_usage events)
    and aggregates by agent.
    """
    events = _read_events(ticket_id, since=0, limit=10000)
    per_agent: dict[str, dict[str, int]] = {}
    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "llm_calls": 0,
    }

    for evt in events:
        if evt.get("event_type") != "llm_usage":
            continue
        data = evt.get("data", {})
        agent = evt.get("agent", "unknown")

        input_tok = data.get("input_tokens", 0)
        output_tok = data.get("output_tokens", 0)

        if agent not in per_agent:
            per_agent[agent] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "llm_calls": 0,
            }
        per_agent[agent]["input_tokens"] += input_tok
        per_agent[agent]["output_tokens"] += output_tok
        per_agent[agent]["llm_calls"] += 1

        total["input_tokens"] += input_tok
        total["output_tokens"] += output_tok
        total["llm_calls"] += 1

    return json.dumps(
        {
            "ticket_id": ticket_id,
            "total": total,
            "by_agent": per_agent,
        },
        indent=2,
    )


@mcp.tool()
async def detect_anomalies(ticket_id: str) -> str:
    """Analyze a ticket's event stream for anomalous patterns."""
    events = _read_events(ticket_id, since=0, limit=10000)
    if not events:
        return json.dumps(
            {
                "ticket_id": ticket_id,
                "anomalies": [],
                "note": "No events found",
            }
        )

    anomalies = _detect_anomalies_from_events(events)
    return json.dumps(
        {
            "ticket_id": ticket_id,
            "total_events": len(events),
            "anomalies": anomalies,
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
