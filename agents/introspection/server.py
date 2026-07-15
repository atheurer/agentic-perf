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
import re
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


def _is_tool_failure(evt: dict[str, Any]) -> bool:
    """Check if a tool_result event represents a failure.

    Looks beyond is_error (which only flags tool handler crashes)
    to detect failures reported in the content JSON: non-zero
    exit codes, success=false, status='failed', or error fields.
    """
    if evt.get("event_type") != "tool_result":
        return False
    data = evt.get("data", {})
    if data.get("is_error"):
        return True
    content = data.get("content", "")
    if not content:
        return False
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, dict):
            if parsed.get("exit_code", 0) != 0:
                return True
            if parsed.get("success") is False:
                return True
            if str(parsed.get("status", "")).lower() in (
                "failed",
                "error",
            ):
                return True
            if parsed.get("error"):
                return True
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return False


def _extract_error_message(evt: dict[str, Any]) -> str:
    """Extract a normalized error message from a failed tool_result."""
    data = evt.get("data", {})
    content = data.get("content", "")
    if not content:
        return ""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, dict):
            for key in ("error", "stderr", "message"):
                val = parsed.get(key)
                if val:
                    return str(val)[:300]
            stdout = parsed.get("stdout", "")
            if stdout:
                return str(stdout)[:300]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return str(content)[:300]


def _classify_error(
    message: str,
    patterns: dict[str, list[re.Pattern[str]]],
) -> str:
    """Classify an error message using skill-loaded patterns.

    Returns 'infrastructure', 'transient', or 'logic'.
    """
    for pattern in patterns.get("infrastructure", []):
        if pattern.search(message):
            return "infrastructure"
    for pattern in patterns.get("transient", []):
        if pattern.search(message):
            return "transient"
    return "logic"


def _error_similarity(msg_a: str, msg_b: str) -> float:
    """Rough similarity between two error messages (Jaccard on words)."""
    if not msg_a or not msg_b:
        return 0.0
    words_a = set(msg_a.lower().split())
    words_b = set(msg_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _detect_anomalies_from_events(
    events: list[dict[str, Any]],
    error_patterns: dict[str, list[re.Pattern[str]]] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Analyze events for anomalous patterns.

    All detection parameters are loaded from introspection skill
    files (skills/introspection/) with private-skills overrides.
    Pass error_patterns and thresholds explicitly for testing;
    None loads from skills at call time.

    Detects:
    - Consecutive tool failures (same tool, similar errors)
    - Repeated tool errors (non-consecutive, total count)
    - Retry loops (same tool + identical input)
    - Max iteration exhaustion
    - Wasted iteration ratio per agent
    """
    from .skills import load_error_patterns, load_thresholds

    if error_patterns is None:
        error_patterns = load_error_patterns()
    if thresholds is None:
        thresholds = load_thresholds()

    # Read thresholds with defaults matching the shipped skill file.
    consec_min = thresholds.get("consecutive_failure_min", 2)
    consec_high = thresholds.get("consecutive_failure_high", 4)
    sim_threshold = thresholds.get("error_similarity_threshold", 0.3)
    repeated_min = thresholds.get("repeated_error_min", 3)
    repeated_high = thresholds.get("repeated_error_high", 5)
    loop_min = thresholds.get("retry_loop_min", 3)
    loop_high = thresholds.get("retry_loop_high", 5)
    waste_min_calls = thresholds.get("wasted_iterations_min_calls", 4)
    waste_min_wasted = thresholds.get("wasted_iterations_min_wasted", 2)
    waste_pct = thresholds.get("wasted_iterations_pct", 25)
    waste_high_pct = thresholds.get("wasted_iterations_high_pct", 50)

    anomalies: list[dict[str, Any]] = []

    # --- Consecutive tool failures with similar errors ---
    streak_tool: str = ""
    streak_errors: list[dict[str, Any]] = []
    streak_msgs: list[str] = []

    def _flush_streak() -> None:
        if len(streak_errors) < consec_min:
            return
        classifications = [_classify_error(m, error_patterns) for m in streak_msgs]
        primary_class = max(
            set(classifications),
            key=classifications.count,
        )
        severity = "high" if len(streak_errors) >= consec_high else "medium"
        sample_msg = streak_msgs[0][:120] if streak_msgs else ""
        desc = f"Tool '{streak_tool}' failed {len(streak_errors)} times consecutively"
        if primary_class == "infrastructure":
            desc += " (infrastructure issue \u2014 retrying won't help)"
        elif primary_class == "transient":
            desc += " (transient \u2014 may resolve on retry)"
        else:
            desc += " (agent may need a different approach)"
        if sample_msg:
            desc += f": {sample_msg}"
        anomalies.append(
            {
                "type": "consecutive_failure",
                "severity": severity,
                "description": desc,
                "error_class": primary_class,
                "seq_range": [
                    streak_errors[0].get("seq", 0),
                    streak_errors[-1].get("seq", 0),
                ],
            }
        )

    for evt in events:
        if evt.get("event_type") != "tool_result":
            continue
        tool = evt.get("data", {}).get("tool", "unknown")
        failed = _is_tool_failure(evt)

        if failed and tool == streak_tool:
            msg = _extract_error_message(evt)
            if (
                not streak_msgs
                or _error_similarity(msg, streak_msgs[-1]) > sim_threshold
            ):
                streak_errors.append(evt)
                streak_msgs.append(msg)
                continue
        _flush_streak()
        if failed:
            streak_tool = tool
            streak_errors = [evt]
            streak_msgs = [_extract_error_message(evt)]
        else:
            streak_tool = ""
            streak_errors = []
            streak_msgs = []

    _flush_streak()

    # --- Total tool errors (including content-based failures) ---
    error_counts: dict[str, list[int]] = {}
    for evt in events:
        if _is_tool_failure(evt):
            tool = evt.get("data", {}).get("tool", "unknown")
            error_counts.setdefault(tool, []).append(evt.get("seq", 0))

    for tool, seqs in error_counts.items():
        if len(seqs) >= repeated_min:
            already = any(
                a["type"] == "consecutive_failure" and tool in a["description"]
                for a in anomalies
            )
            if not already:
                anomalies.append(
                    {
                        "type": "repeated_error",
                        "severity": (
                            "high" if len(seqs) >= repeated_high else "medium"
                        ),
                        "description": (f"Tool '{tool}' failed {len(seqs)} times"),
                        "seq_range": [seqs[0], seqs[-1]],
                    }
                )

    # --- Retry loops (same tool + identical input) ---
    prev_call: dict[str, Any] | None = None
    loop_count = 1
    loop_start_seq = 0
    for evt in events:
        if evt.get("event_type") != "tool_called":
            continue
        data = evt.get("data", {})
        current = {
            "tool": data.get("tool"),
            "input": json.dumps(
                data.get("input", {}),
                sort_keys=True,
            ),
        }
        if prev_call and current == prev_call:
            loop_count += 1
        else:
            if loop_count >= loop_min and prev_call:
                anomalies.append(
                    {
                        "type": "retry_loop",
                        "severity": ("high" if loop_count >= loop_high else "medium"),
                        "description": (
                            f"Tool '{prev_call['tool']}' called "
                            f"{loop_count} times with identical"
                            f" input"
                        ),
                        "seq_range": [
                            loop_start_seq,
                            evt.get("seq", 0) - 1,
                        ],
                    }
                )
            loop_count = 1
            loop_start_seq = evt.get("seq", 0)
        prev_call = current

    if loop_count >= loop_min and prev_call:
        anomalies.append(
            {
                "type": "retry_loop",
                "severity": ("high" if loop_count >= loop_high else "medium"),
                "description": (
                    f"Tool '{prev_call['tool']}' called "
                    f"{loop_count} times with identical input"
                ),
                "seq_range": [
                    loop_start_seq,
                    events[-1].get("seq", 0),
                ],
            }
        )

    # --- Max iterations ---
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

    # --- Wasted iteration ratio per agent ---
    agent_llm_calls: dict[str, int] = {}
    agent_wasted: dict[str, int] = {}
    cur_agent = ""
    cur_had_success = False
    cur_had_failure = False

    for evt in events:
        etype = evt.get("event_type", "")
        agent = evt.get("agent", "")

        if etype == "llm_request":
            if cur_agent:
                agent_llm_calls[cur_agent] = agent_llm_calls.get(cur_agent, 0) + 1
                if cur_had_failure and not cur_had_success:
                    agent_wasted[cur_agent] = agent_wasted.get(cur_agent, 0) + 1
            cur_agent = agent
            cur_had_success = False
            cur_had_failure = False
        elif etype == "tool_result" and agent == cur_agent:
            if _is_tool_failure(evt):
                cur_had_failure = True
            else:
                cur_had_success = True

    if cur_agent:
        agent_llm_calls[cur_agent] = agent_llm_calls.get(cur_agent, 0) + 1
        if cur_had_failure and not cur_had_success:
            agent_wasted[cur_agent] = agent_wasted.get(cur_agent, 0) + 1

    for agent, wasted in agent_wasted.items():
        total = agent_llm_calls.get(agent, 0)
        if total < waste_min_calls or wasted < waste_min_wasted:
            continue
        pct = round(100 * wasted / total)
        if pct >= waste_pct:
            anomalies.append(
                {
                    "type": "wasted_iterations",
                    "severity": ("high" if pct >= waste_high_pct else "medium"),
                    "description": (
                        f"Agent '{agent}': {wasted}/{total}"
                        f" LLM calls ({pct}%) produced only"
                        f" failed tool results"
                    ),
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
