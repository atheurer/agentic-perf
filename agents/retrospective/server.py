from __future__ import annotations

import json
import re
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("retrospective")

DEFAULT_LOG_DIR = Path.home() / ".agentic-perf" / "logs"

SELF_CORRECTION_PATTERNS = [
    re.compile(r"\blet me try\b", re.IGNORECASE),
    re.compile(r"\bthat didn'?t work\b", re.IGNORECASE),
    re.compile(r"\btry a different\b", re.IGNORECASE),
    re.compile(r"\btry again\b", re.IGNORECASE),
    re.compile(r"\berror occurred\b", re.IGNORECASE),
    re.compile(r"\bfailed[.,;:\s]", re.IGNORECASE),
    re.compile(r"\binstead[.,;:\s]", re.IGNORECASE),
]


def _read_transcript(ticket_id: str) -> list[dict]:
    path = DEFAULT_LOG_DIR / f"{ticket_id}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _get_context(events: list[dict], idx: int, radius: int = 2) -> list[dict]:
    """Return surrounding events for context, stripping large payloads."""
    start = max(0, idx - radius)
    end = min(len(events), idx + radius + 1)
    context = []
    for e in events[start:end]:
        trimmed = {
            "seq": e.get("seq"),
            "agent": e.get("agent"),
            "event_type": e.get("event_type"),
        }
        data = e.get("data", {})
        if e.get("event_type") == "llm_response":
            trimmed["data"] = {
                "iteration": data.get("iteration"),
                "stop_reason": data.get("stop_reason"),
                "tool_calls": data.get("tool_calls", []),
                "text": (data.get("text") or "")[:300],
            }
        elif e.get("event_type") == "tool_called":
            trimmed["data"] = {
                "tool": data.get("tool"),
                "input": _truncate_value(data.get("input"), 500),
            }
        elif e.get("event_type") == "tool_result":
            trimmed["data"] = {
                "tool": data.get("tool"),
                "is_error": data.get("is_error"),
                "content": (data.get("content") or "")[:500],
            }
        else:
            trimmed["data"] = {
                k: _truncate_value(v, 200) for k, v in data.items()
            }
        context.append(trimmed)
    return context


def _truncate_value(val, max_len: int = 200):
    if val is None:
        return None
    s = json.dumps(val, default=str) if not isinstance(val, str) else val
    if len(s) > max_len:
        return s[:max_len] + "..."
    return val


def _extract_signals(events: list[dict]) -> list[dict]:
    """Extract all signals from a transcript."""
    signals = []

    for i, evt in enumerate(events):
        etype = evt.get("event_type")
        data = evt.get("data", {})
        agent = evt.get("agent", "")

        # Tool errors
        if etype == "tool_result" and data.get("is_error"):
            signals.append({
                "type": "tool_error",
                "agent": agent,
                "tool": data.get("tool"),
                "error": (data.get("content") or "")[:500],
                "seq": evt.get("seq"),
                "context": _get_context(events, i),
            })

        # Max iterations
        if (
            etype == "agent_error"
            and data.get("reason") == "max_iterations"
        ):
            signals.append({
                "type": "max_iterations",
                "agent": agent,
                "seq": evt.get("seq"),
                "context": _get_context(events, i),
            })

        # HITL escalations
        if (
            etype == "transition"
            and data.get("to") == "awaiting_customer_guidance"
        ):
            signals.append({
                "type": "hitl_escalation",
                "agent": agent,
                "comment": data.get("comment", ""),
                "seq": evt.get("seq"),
                "context": _get_context(events, i),
            })

        # Self-correction language
        if etype == "llm_response":
            text = data.get("text") or ""
            matched = [
                p.pattern for p in SELF_CORRECTION_PATTERNS
                if p.search(text)
            ]
            if matched:
                signals.append({
                    "type": "self_correction",
                    "agent": agent,
                    "patterns": matched,
                    "text_snippet": text[:300],
                    "seq": evt.get("seq"),
                    "context": _get_context(events, i, radius=1),
                })

    # Retry sequences (consecutive tool_called for same tool, 3+)
    tool_calls = [
        (i, evt)
        for i, evt in enumerate(events)
        if evt.get("event_type") == "tool_called"
    ]
    streak_start = 0
    for j in range(1, len(tool_calls) + 1):
        same = (
            j < len(tool_calls)
            and tool_calls[j][1].get("data", {}).get("tool")
            == tool_calls[streak_start][1].get("data", {}).get("tool")
            and tool_calls[j][1].get("agent")
            == tool_calls[streak_start][1].get("agent")
        )
        if not same:
            streak_len = j - streak_start
            if streak_len >= 3:
                first_idx = tool_calls[streak_start][0]
                last_idx = tool_calls[j - 1][0]
                tool_name = (
                    tool_calls[streak_start][1]
                    .get("data", {})
                    .get("tool", "?")
                )
                signals.append({
                    "type": "retry_sequence",
                    "agent": tool_calls[streak_start][1].get("agent", ""),
                    "tool": tool_name,
                    "count": streak_len,
                    "seq_range": [
                        events[first_idx].get("seq"),
                        events[last_idx].get("seq"),
                    ],
                    "context": _get_context(
                        events, first_idx, radius=1
                    ) + _get_context(events, last_idx, radius=1),
                })
            streak_start = j

    # Fail-then-succeed patterns
    results = [
        (i, evt)
        for i, evt in enumerate(events)
        if evt.get("event_type") == "tool_result"
    ]
    for j in range(len(results) - 1):
        idx_a, evt_a = results[j]
        idx_b, evt_b = results[j + 1]
        data_a = evt_a.get("data", {})
        data_b = evt_b.get("data", {})
        if (
            data_a.get("is_error")
            and not data_b.get("is_error")
            and data_a.get("tool") == data_b.get("tool")
        ):
            signals.append({
                "type": "fail_then_succeed",
                "agent": evt_a.get("agent", ""),
                "tool": data_a.get("tool"),
                "error": (data_a.get("content") or "")[:300],
                "seq_range": [evt_a.get("seq"), evt_b.get("seq")],
                "context": _get_context(events, idx_a)
                + _get_context(events, idx_b),
            })

    return signals


def _compute_stats(events: list[dict]) -> dict:
    """Compute per-agent statistics from the transcript."""
    by_agent: dict[str, dict] = {}
    for evt in events:
        agent = evt.get("agent", "")
        if not agent:
            continue
        if agent not in by_agent:
            by_agent[agent] = {
                "iterations": 0,
                "tool_calls": 0,
                "tool_errors": 0,
            }
        stats = by_agent[agent]
        etype = evt.get("event_type")
        if etype == "llm_request":
            stats["iterations"] += 1
        elif etype == "tool_called":
            stats["tool_calls"] += 1
        elif etype == "tool_result" and evt.get("data", {}).get("is_error"):
            stats["tool_errors"] += 1

    return {
        "total_events": len(events),
        "by_agent": by_agent,
    }


@mcp.tool()
def get_transcript_analysis(ticket_id: str) -> dict:
    """Analyze a ticket transcript and return detected signals."""
    events = _read_transcript(ticket_id)
    if not events:
        return {
            "ticket_id": ticket_id,
            "error": f"No transcript found for {ticket_id}",
            "signals": [],
            "stats": {"total_events": 0, "by_agent": {}},
        }

    signals = _extract_signals(events)
    stats = _compute_stats(events)

    return {
        "ticket_id": ticket_id,
        "signals": signals,
        "stats": stats,
    }


if __name__ == "__main__":
    mcp.run()
