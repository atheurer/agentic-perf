"""Introspection detection engine.

Provides deterministic anomaly detection, tool failure parsing,
event reading, and error classification for the introspection
agent.  All detection parameters are loaded from skill files
(skills/introspection/) with private-skills overrides.
"""

from __future__ import annotations

import json
import re
from typing import Any

from paths import LOG_DIR as DEFAULT_LOG_DIR


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
            line_num += 1
            if line_num <= since:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt["seq"] = line_num
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
            err_val = parsed.get("error")
            if err_val and str(err_val).lower() not in (
                "none",
                "null",
                "n/a",
                "false",
            ):
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


# Noise patterns stripped before Jaccard similarity comparison.
# Hex addresses, line numbers, timestamps, and UUIDs vary between
# otherwise-identical errors and dilute the word overlap score.
_SIMILARITY_NOISE = re.compile(
    r"0x[0-9a-fA-F]+"  # hex addresses
    r"|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[:\d.]*\b"  # timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|\bline \d+\b"  # line references
    r"|:\d+:"  # file:lineno: patterns
    r"|\b\d{5,}\b",  # large numbers (PIDs, ports above 9999)
)


def _sanitize_for_similarity(msg: str) -> str:
    """Strip noise tokens that vary between same-cause errors."""
    return _SIMILARITY_NOISE.sub("", msg)


def _error_similarity(msg_a: str, msg_b: str) -> float:
    """Rough similarity between two error messages (Jaccard on words).

    Strips hex addresses, timestamps, UUIDs, line numbers, and
    large numeric values before comparison so that long
    tracebacks with varying context don't dilute the score.
    """
    if not msg_a or not msg_b:
        return 0.0
    clean_a = _sanitize_for_similarity(msg_a)
    clean_b = _sanitize_for_similarity(msg_b)
    words_a = set(clean_a.lower().split())
    words_b = set(clean_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _detect_anomalies_from_events(
    events: list[dict[str, Any]],
    error_patterns: dict[str, list[re.Pattern[str]]] | None = None,
    thresholds: dict[str, Any] | None = None,
    bypass_patterns: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Analyze events for anomalous patterns.

    All detection parameters are loaded from introspection skill
    files (skills/introspection/) with private-skills overrides.
    Pass error_patterns, thresholds, and bypass_patterns explicitly
    for testing; None loads from skills at call time.

    Detects:
    - Consecutive tool failures (same tool, similar errors)
    - Repeated tool errors (non-consecutive, total count)
    - Retry loops (same tool + identical input)
    - Max iteration exhaustion
    - Wasted iteration ratio per agent
    - Tool bypass patterns (generic tool used instead of
      specialized tool, manual schema exploration, manual
      container orchestration)
    """
    from .skills import load_error_patterns, load_thresholds, load_tool_bypass_patterns

    if error_patterns is None:
        error_patterns = load_error_patterns()
    if bypass_patterns is None:
        bypass_patterns = load_tool_bypass_patterns()
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
    # Track per-tool so interleaved diagnostic tools (get_status,
    # check_os) don't reset streaks for the failing tool.
    tool_streaks: dict[str, list[dict[str, Any]]] = {}
    tool_streak_msgs: dict[str, list[str]] = {}

    def _flush_streak(tool: str) -> None:
        errors = tool_streaks.get(tool, [])
        msgs = tool_streak_msgs.get(tool, [])
        if len(errors) < consec_min:
            return
        classifications = [_classify_error(m, error_patterns) for m in msgs]
        primary_class = max(
            set(classifications),
            key=classifications.count,
        )
        severity = "high" if len(errors) >= consec_high else "medium"
        sample_msg = msgs[0][:120] if msgs else ""
        desc = f"Tool '{tool}' failed {len(errors)} times consecutively"
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
                    errors[0].get("seq", 0),
                    errors[-1].get("seq", 0),
                ],
            }
        )

    for evt in events:
        if evt.get("event_type") != "tool_result":
            continue
        tool = evt.get("data", {}).get("tool", "unknown")
        failed = _is_tool_failure(evt)

        if failed:
            msg = _extract_error_message(evt)
            existing_msgs = tool_streak_msgs.get(tool, [])
            if (
                not existing_msgs
                or _error_similarity(
                    msg,
                    existing_msgs[-1],
                )
                > sim_threshold
            ):
                tool_streaks.setdefault(tool, []).append(evt)
                tool_streak_msgs.setdefault(tool, []).append(
                    msg,
                )
            else:
                # Error changed character — flush old streak,
                # start new one for this tool.
                _flush_streak(tool)
                tool_streaks[tool] = [evt]
                tool_streak_msgs[tool] = [msg]
        else:
            # Success for this tool resets its streak only.
            _flush_streak(tool)
            tool_streaks.pop(tool, None)
            tool_streak_msgs.pop(tool, None)

    for tool in list(tool_streaks):
        _flush_streak(tool)

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
    # Track per-tool so interleaved diagnostic tools don't
    # reset loop counters for the retried tool.
    tool_prev_input: dict[str, str] = {}
    tool_loop_count: dict[str, int] = {}
    tool_loop_start: dict[str, int] = {}
    tool_loop_end: dict[str, int] = {}

    def _flush_loop(tool: str) -> None:
        count = tool_loop_count.get(tool, 0)
        if count >= loop_min:
            anomalies.append(
                {
                    "type": "retry_loop",
                    "severity": ("high" if count >= loop_high else "medium"),
                    "description": (
                        f"Tool '{tool}' called {count} times with identical input"
                    ),
                    "seq_range": [
                        tool_loop_start.get(tool, 0),
                        tool_loop_end.get(tool, 0),
                    ],
                }
            )

    for evt in events:
        if evt.get("event_type") != "tool_called":
            continue
        data = evt.get("data", {})
        tool = data.get("tool", "unknown")
        input_key = json.dumps(
            data.get("input", {}),
            sort_keys=True,
        )
        seq = evt.get("seq", 0)

        prev_input = tool_prev_input.get(tool)
        if prev_input is not None and input_key == prev_input:
            tool_loop_count[tool] = tool_loop_count.get(tool, 1) + 1
            tool_loop_end[tool] = seq
        else:
            _flush_loop(tool)
            tool_prev_input[tool] = input_key
            tool_loop_count[tool] = 1
            tool_loop_start[tool] = seq
            tool_loop_end[tool] = seq

    for tool in list(tool_loop_count):
        _flush_loop(tool)

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

    # --- Tool bypass patterns ---
    bypass_min = thresholds.get("tool_bypass_min_calls", 3)
    tool_mappings = bypass_patterns.get("tool_mappings", [])
    cmd_patterns = bypass_patterns.get("command_patterns", [])

    # Per-agent tool call counts for generic-vs-specialized check.
    agent_tool_counts: dict[str, dict[str, int]] = {}
    # Per-agent command pattern matches.
    agent_cmd_matches: dict[str, list[dict[str, Any]]] = {}

    for evt in events:
        if evt.get("event_type") != "tool_called":
            continue
        agent = evt.get("agent", "")
        data = evt.get("data", {})
        tool = data.get("tool", "")
        if not agent or not tool:
            continue

        agent_tool_counts.setdefault(agent, {})
        agent_tool_counts[agent][tool] = agent_tool_counts[agent].get(tool, 0) + 1

        # Check command content patterns.
        for cp in cmd_patterns:
            if not agent.startswith(cp["agent"]):
                continue
            if tool != cp["tool"]:
                continue
            input_str = json.dumps(data.get("input", {}), default=str)
            if cp["pattern"].search(input_str):
                agent_cmd_matches.setdefault(agent, [])
                agent_cmd_matches[agent].append(
                    {
                        "seq": evt.get("seq", 0),
                        "description": cp["description"],
                        "severity": cp["severity"],
                    }
                )

    # Detect generic-tool-instead-of-specialized-tool.
    for mapping in tool_mappings:
        if not isinstance(mapping, dict):
            continue
        agent_prefix = mapping.get("agent", "")
        generic = mapping.get("generic_tool", "")
        specialized = mapping.get("specialized_tool", "")
        if not agent_prefix or not generic or not specialized:
            continue

        for agent, counts in agent_tool_counts.items():
            if not agent.startswith(agent_prefix):
                continue
            generic_count = counts.get(generic, 0)
            specialized_count = counts.get(specialized, 0)
            if generic_count >= bypass_min and specialized_count == 0:
                anomalies.append(
                    {
                        "type": "tool_bypass",
                        "severity": "high",
                        "description": (
                            f"Agent '{agent}' called"
                            f" '{generic}' {generic_count}"
                            f" times without calling"
                            f" '{specialized}' \u2014"
                            f" {mapping.get('description', 'possible tool bypass')}"
                        ),
                    }
                )

    # Aggregate command-pattern matches per description.
    seen_descs: set[str] = set()
    for agent, matches in agent_cmd_matches.items():
        by_desc: dict[str, list[dict[str, Any]]] = {}
        for m in matches:
            by_desc.setdefault(m["description"], []).append(m)
        for desc, group in by_desc.items():
            key = f"{agent}:{desc}"
            if key in seen_descs:
                continue
            seen_descs.add(key)
            anomalies.append(
                {
                    "type": "tool_bypass",
                    "severity": group[0]["severity"],
                    "description": (
                        f"Agent '{agent}': {desc}"
                        f" ({len(group)} occurrence"
                        f"{'s' if len(group) != 1 else ''})"
                    ),
                    "seq_range": [
                        group[0]["seq"],
                        group[-1]["seq"],
                    ],
                }
            )

    return anomalies
