#!/usr/bin/env python3
"""Agentic Perf CLI — interact with tickets and agents."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_STORE_URL = "http://localhost:8090"


def show_disclaimer():
    """Show AI safety disclaimer once per session."""
    if os.environ.get("AGENTIC_PERF_DISCLAIMER_SHOWN"):
        return

    print(
        "\n⚠️  AI-generated content may contain errors. Always verify before acting.\n"
    )
    os.environ["AGENTIC_PERF_DISCLAIMER_SHOWN"] = "1"


def get_client(args) -> tuple[httpx.Client, str]:
    url = args.store_url.rstrip("/")
    return httpx.Client(base_url=url, timeout=10.0), url


def cmd_submit(args):
    client, url = get_client(args)
    description = args.description or args.summary
    r = client.post(
        "/api/v1/tickets",
        json={
            "summary": args.summary,
            "description": description,
        },
    )
    r.raise_for_status()
    ticket = r.json()
    tid = ticket["id"]

    r = client.post(
        f"/api/v1/tickets/{tid}/transition", json={"status": "triage_pending"}
    )
    r.raise_for_status()

    print(f"Created ticket: {tid}")
    print("Status: triage_pending")
    print(f"Summary: {args.summary}")


def cmd_list(args):
    client, url = get_client(args)
    params = {}
    if args.status:
        params["status"] = args.status
    r = client.get("/api/v1/tickets", params=params)
    r.raise_for_status()
    tickets = r.json()

    if not tickets:
        print("No tickets found.")
        return

    for t in tickets:
        status = t["status"]
        summary = t["summary"][:60]
        print(f"  {t['id']}  {status:30s}  {summary}")


def cmd_show(args):
    client, url = get_client(args)
    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    t = r.json()

    print()
    print("=" * 80)
    print(f"  {t['id']}  —  {t['status'].upper()}")
    print("=" * 80)
    print()
    print(f"  Summary: {t['summary']}")
    print()

    cf = t.get("custom_fields", {})
    if cf:
        print("— Fields " + "—" * 70)
        for key, val in sorted(cf.items()):
            if isinstance(val, (dict, list)):
                s = json.dumps(val, indent=2)
                if len(s) > 300:
                    s = s[:300] + "\n  ...(truncated)"
                print(f"  {key}:")
                for line in s.split("\n"):
                    print(f"    {line}")
            elif isinstance(val, str):
                if "\n" in val:
                    print(f"  {key}:")
                    for line in val.split("\n"):
                        print(f"    {line}")
                else:
                    print(f"  {key}: {val}")
            else:
                print(f"  {key}: {val}")
        print()

    comments = t.get("comments", [])
    if comments:
        print("— Comments " + "—" * 68)
        for i, c in enumerate(comments, 1):
            print(f"  [{i}] {c['author']}:")
            for line in c["body"].split("\n"):
                print(f"      {line}")
            print()


EVENT_ICONS = {
    "agent_started": "+",
    "agent_finished": "-",
    "agent_error": "!",
    "llm_request": ">",
    "llm_response": "<",
    "tool_called": "~",
    "tool_result": "=",
    "tool_skipped": "x",
    "transition": "->",
    "comment": "#",
}


def _read_events(ticket_id, last_seq):
    log_path = Path.home() / ".agentic-perf" / "logs" / f"{ticket_id}.jsonl"
    if not log_path.exists():
        return [], last_seq
    events = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("seq", 0) > last_seq:
                events.append(evt)
    new_seq = events[-1]["seq"] if events else last_seq
    return events, new_seq


def _format_event(evt):
    etype = evt["event_type"]
    icon = EVENT_ICONS.get(etype, "?")
    agent = evt.get("agent", "")
    data = evt.get("data", {})
    ts = evt.get("timestamp", "")
    if "T" in ts:
        ts = ts.split("T")[1][:8]

    if etype == "agent_started":
        return f"  [{ts}] {icon} {agent} started"
    elif etype == "agent_finished":
        return f"  [{ts}] {icon} {agent} finished"
    elif etype == "agent_error":
        return f"  [{ts}] {icon} {agent} ERROR: {data.get('reason', '?')}"
    elif etype == "llm_request":
        return f"  [{ts}] {icon} {agent} LLM request (iteration {data.get('iteration', '?')})"
    elif etype == "llm_response":
        tools = data.get("tool_calls", [])
        stop = data.get("stop_reason", "?")
        if tools:
            return f"  [{ts}] {icon} {agent} LLM -> {stop}, tools: {', '.join(tools)}"
        return f"  [{ts}] {icon} {agent} LLM -> {stop} (text: {data.get('text_length', 0)} chars)"
    elif etype == "tool_called":
        return f"  [{ts}] {icon} {agent} calling {data.get('tool', '?')}"
    elif etype == "tool_result":
        err = " ERROR" if data.get("is_error") else ""
        return f"  [{ts}] {icon} {agent} {data.get('tool', '?')}{err} ({data.get('content_length', 0)} bytes)"
    elif etype == "tool_skipped":
        return f"  [{ts}] {icon} {agent} skipped {data.get('tool', '?')}: {data.get('reason', '')}"
    elif etype == "transition":
        return f"  [{ts}] {icon} {agent} -> {data.get('to', '?')}"
    elif etype == "comment":
        body = data.get("body", "")[:80]
        return f"  [{ts}] {icon} {agent}: {body}"
    return f"  [{ts}] {icon} {agent} {etype}: {data}"


def cmd_watch(args):
    client, url = get_client(args)
    last_comment_count = 0
    last_status = None
    last_event_seq = 0
    verbose = getattr(args, "verbose", False)

    print(f"Watching ticket {args.ticket_id} (Ctrl+C to stop)")
    if verbose:
        print("  Verbose mode: reading events from ~/.agentic-perf/logs/")
    print()

    try:
        while True:
            r = client.get(f"/api/v1/tickets/{args.ticket_id}")
            r.raise_for_status()
            t = r.json()

            status = t["status"]
            comments = t.get("comments", [])

            if verbose:
                events, last_event_seq = _read_events(args.ticket_id, last_event_seq)
                for evt in events:
                    print(_format_event(evt))
            else:
                if status != last_status:
                    print(f"  [{time.strftime('%H:%M:%S')}] Status: {status}")
                    last_status = status

                while last_comment_count < len(comments):
                    c = comments[last_comment_count]
                    first_line = c["body"].split("\n")[0][:80]
                    print(
                        f"  [{time.strftime('%H:%M:%S')}] {c['author']}: {first_line}"
                    )
                    last_comment_count += 1

            if status in ("closed",):
                print()
                print("  Ticket closed.")
                break

            if status == "awaiting_customer_guidance":
                print()
                print("  >>> Agent is waiting for your input.")
                print(f'  >>> Use: agentic-perf reply {args.ticket_id} "your response"')
                print(
                    f"  >>> Or:  agentic-perf abort {args.ticket_id} to skip to cleanup"
                )
                if not args.follow:
                    break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  Stopped watching.")


def cmd_reply(args):
    client, url = get_client(args)

    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    t = r.json()

    if t["status"] != "awaiting_customer_guidance":
        print(f"Ticket is not waiting for input (status: {t['status']})")
        return

    r = client.post(
        f"/api/v1/tickets/{args.ticket_id}/comments",
        json={
            "author": "user",
            "body": args.message,
        },
    )
    r.raise_for_status()

    if args.abort:
        r = client.post(
            f"/api/v1/tickets/{args.ticket_id}/transition",
            json={
                "status": "awaiting_teardown",
                "comment": "User requested abort, skipping to cleanup",
            },
        )
        r.raise_for_status()
        print("Reply added and ticket aborted — moving to teardown.")
        return

    previous = t.get("previous_status")
    if not previous:
        print("Warning: no previous_status recorded, cannot resume automatically.")
        return

    r = client.post(
        f"/api/v1/tickets/{args.ticket_id}/transition",
        json={
            "status": previous,
            "comment": "User responded, resuming pipeline",
        },
    )
    r.raise_for_status()

    print(f"Reply added and ticket resumed to: {previous}")


def cmd_approve(args):
    client, _ = get_client(args)

    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    t = r.json()

    pa = t.get("custom_fields", {}).get("pending_approval")
    if not pa or pa.get("status") != "pending":
        print("No pending approval request on this ticket.")
        return

    print(f"  Agent:   {pa.get('agent', '?')}")
    print(f"  Host:    {pa.get('host', '?')}")
    print(f"  Binary:  {pa.get('binary', '?')}")
    print(f"  Command: {pa.get('command', '?')}")

    if args.ticket:
        decision = "approved_ticket"
        label = f"Approved '{pa['binary']}' for this ticket"
    else:
        decision = "approved_once"
        label = "Approved (once)"

    pa["status"] = decision
    fields = {"pending_approval": pa}

    if args.ticket:
        approvals = t.get("custom_fields", {}).get("command_approvals", [])
        binary = pa.get("binary", "")
        if binary and binary not in approvals:
            approvals.append(binary)
        fields["command_approvals"] = approvals

    r = client.patch(
        f"/api/v1/tickets/{args.ticket_id}/fields",
        json={"fields": fields},
    )
    r.raise_for_status()
    print(f"{label}")


def cmd_deny(args):
    client, _ = get_client(args)

    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    t = r.json()

    pa = t.get("custom_fields", {}).get("pending_approval")
    if not pa or pa.get("status") != "pending":
        print("No pending approval request on this ticket.")
        return

    print(f"  Denied: {pa.get('binary', '?')} — {pa.get('command', '?')[:80]}")

    pa["status"] = "denied"
    r = client.patch(
        f"/api/v1/tickets/{args.ticket_id}/fields",
        json={"fields": {"pending_approval": pa}},
    )
    r.raise_for_status()
    print("Command denied.")


def cmd_abort(args):
    client, url = get_client(args)

    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    t = r.json()

    if t["status"] != "awaiting_customer_guidance":
        print(f"Ticket is not waiting for input (status: {t['status']})")
        print(
            "Abort is only available when the ticket is in awaiting_customer_guidance."
        )
        return

    reason = args.reason or "User requested abort"
    r = client.post(
        f"/api/v1/tickets/{args.ticket_id}/comments",
        json={
            "author": "user",
            "body": f"**Abort requested:** {reason}",
        },
    )
    r.raise_for_status()

    r = client.post(
        f"/api/v1/tickets/{args.ticket_id}/transition",
        json={
            "status": "awaiting_teardown",
            "comment": "User requested abort, skipping to cleanup",
        },
    )
    r.raise_for_status()

    print(f"Ticket {args.ticket_id} aborted — moving to teardown.")


def cmd_stop(args):
    client, url = get_client(args)

    mode = "hard" if args.hard else "graceful"
    r = client.post(
        f"/api/v1/tickets/{args.ticket_id}/stop",
        json={"mode": mode},
    )
    if r.status_code == 404:
        print(f"Ticket {args.ticket_id} not found.")
        return
    if r.status_code == 409:
        print(r.json().get("detail", "Ticket is already in a terminal state."))
        return
    r.raise_for_status()

    label = "Hard stop" if args.hard else "Graceful stop"
    print(f"{label} requested for ticket {args.ticket_id}.")


def cmd_stop_all(args):
    client, url = get_client(args)

    mode = "hard" if args.hard else "graceful"

    if not args.yes:
        r = client.get("/api/v1/health")
        r.raise_for_status()
        total = r.json().get("total", 0)
        label = "hard-stop" if args.hard else "stop"
        confirm = input(
            f"This will {label} all active tickets"
            f" ({total} total). Continue? [y/N] "
        )
        if confirm.lower() not in ("y", "yes"):
            print("Cancelled.")
            return

    r = client.post("/api/v1/stop-all", json={"mode": mode})
    r.raise_for_status()
    data = r.json()
    count = data.get("count", 0)
    label = "Hard stop" if args.hard else "Graceful stop"
    print(f"{label} requested for {count} active ticket(s).")


def _load_aws_config() -> dict:
    config_path = Path.home() / ".agentic-perf" / "secrets" / "aws" / "config.json"
    if not config_path.exists():
        print(f"AWS config not found: {config_path}")
        sys.exit(1)
    return json.loads(config_path.read_text())


def _get_tag(inst: dict, key: str) -> str:
    for tag in inst.get("Tags", []):
        if tag["Key"] == key:
            return tag["Value"]
    return ""


def _format_age(launch_time) -> str:
    delta = datetime.now(timezone.utc) - launch_time
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def cmd_cleanup(args):
    import boto3

    config = _load_aws_config()
    kwargs = {
        "region_name": config["region"],
        "aws_access_key_id": config["access_key_id"],
        "aws_secret_access_key": config["secret_access_key"],
    }
    if config.get("session_token"):
        kwargs["aws_session_token"] = config["session_token"]

    ec2 = boto3.client("ec2", **kwargs)

    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:agentic-perf", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )

    instances = []
    for reservation in response["Reservations"]:
        for inst in reservation["Instances"]:
            instances.append(inst)

    if args.older_than:
        cutoff = datetime.now(timezone.utc).timestamp() - (args.older_than * 3600)
        instances = [i for i in instances if i["LaunchTime"].timestamp() < cutoff]

    if not instances:
        print("No matching agentic-perf instances found.")
        return

    print(f"Found {len(instances)} agentic-perf instance(s) in {config['region']}:\n")
    print(f"  {'Instance ID':<20} {'State':<10} {'Age':>6}  {'Ticket':<16} {'Name'}")
    print(f"  {'─' * 20} {'─' * 10} {'─' * 6}  {'─' * 16} {'─' * 30}")
    for inst in instances:
        print(
            f"  {inst['InstanceId']:<20} "
            f"{inst['State']['Name']:<10} "
            f"{_format_age(inst['LaunchTime']):>6}  "
            f"{_get_tag(inst, 'ticket-id') or '—':<16} "
            f"{_get_tag(inst, 'Name')}"
        )

    if not args.terminate:
        return

    if not args.yes:
        [i["InstanceId"] for i in instances]
        answer = input(f"\nTerminate {len(instances)} instance(s)? [y/N] ")
        if answer.lower() not in ("y", "yes"):
            print("Aborted.")
            return

    instance_ids = [i["InstanceId"] for i in instances]
    result = ec2.terminate_instances(InstanceIds=instance_ids)
    for i in result.get("TerminatingInstances", []):
        print(
            f"  {i['InstanceId']}: {i['PreviousState']['Name']} → {i['CurrentState']['Name']}"
        )
    print(f"\nTerminated {len(instance_ids)} instance(s).")


def _read_all_events(ticket_id):
    log_path = Path.home() / ".agentic-perf" / "logs" / f"{ticket_id}.jsonl"
    if not log_path.exists():
        return []
    events = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _ts_short(ts):
    if "T" in ts:
        return ts.split("T")[1][:8]
    return ts


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in text.split("\n"))


def _format_tool_input(input_dict):
    if not input_dict:
        return "()"
    parts = []
    for k, v in input_dict.items():
        if isinstance(v, str) and len(v) > 200:
            v_str = repr(v[:200] + "...")
        elif isinstance(v, (dict, list)):
            s = json.dumps(v, indent=2, default=str)
            if len(s) > 300:
                s = s[:300] + "\n...(truncated)"
            v_str = s
        else:
            v_str = repr(v)
        parts.append(f"{k}={v_str}")
    return "(\n" + _indent(",\n".join(parts)) + "\n  )"


def _render_transcript(events, ticket, agent_filter=None):
    tid = ticket["id"]
    status = ticket["status"]
    created = ticket.get("created_at", "?")

    print()
    print("═" * 70)
    print(f"  {tid} — Full Transcript")
    print(f"  Status: {status.upper()}")
    print(f"  Created: {created}")
    print(f"  Summary: {ticket['summary']}")
    print("═" * 70)
    print()

    print("USER REQUEST:")
    print(_indent(ticket["description"]))
    print()

    current_agent = None

    for evt in events:
        etype = evt["event_type"]
        agent = evt.get("agent", "")
        data = evt.get("data", {})
        ts = _ts_short(evt.get("timestamp", ""))

        if agent_filter and agent != agent_filter:
            continue

        if etype == "agent_started":
            if agent != current_agent:
                current_agent = agent
                print("─" * 70)
                print(f"  {agent.upper()}")
                sys_prompt = data.get("system_prompt", "")
                if sys_prompt:
                    preview = sys_prompt[:200]
                    if len(sys_prompt) > 200:
                        preview += "..."
                    print(f"  System Prompt: {preview}")
                print("─" * 70)
                print()

            initial = data.get("initial_messages", [])
            for msg in initial:
                role = msg.get("role", "?").upper()
                content = msg.get("content", "")
                if isinstance(content, str):
                    print(f"  [{ts}] {role} MESSAGE →")
                    print(_indent(content))
                    print()

        elif etype == "llm_response":
            iteration = data.get("iteration", "?")
            stop = data.get("stop_reason", "?")
            text = data.get("text")
            raw = data.get("raw_content", [])

            print(f"  [{ts}] LLM RESPONSE (iteration {iteration}, stop={stop}) →")
            if text:
                print(_indent(text))

            tool_calls_in_raw = [
                block
                for block in (raw or [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if tool_calls_in_raw:
                for tc_block in tool_calls_in_raw:
                    name = tc_block.get("name", "?")
                    inp = tc_block.get("input", {})
                    print(f"    ┌─ TOOL CALL: {name}{_format_tool_input(inp)}")
            print()

        elif etype == "tool_called":
            tool = data.get("tool", "?")
            inp = data.get("input")
            if inp is not None:
                print(f"  [{ts}] TOOL CALL: {tool}{_format_tool_input(inp)}")
            else:
                keys = data.get("input_keys", [])
                print(f"  [{ts}] TOOL CALL: {tool}({', '.join(keys)})")

        elif etype == "tool_result":
            tool = data.get("tool", "?")
            is_error = data.get("is_error", False)
            content = data.get("content")
            content_length = data.get("content_length", 0)
            err_tag = " ERROR" if is_error else ""

            if content is not None:
                try:
                    parsed = json.loads(content)
                    formatted = json.dumps(parsed, indent=2, default=str)
                except (json.JSONDecodeError, TypeError):
                    formatted = content
                if len(formatted) > 2000:
                    formatted = formatted[:2000] + "\n  ...(truncated)"
                print(f"  [{ts}] TOOL RESULT{err_tag}: {tool} →")
                print(_indent(formatted))
            else:
                print(f"  [{ts}] TOOL RESULT{err_tag}: {tool} ({content_length} bytes)")
            print()

        elif etype == "transition":
            to = data.get("to", "?")
            comment = data.get("comment", "")
            print(f"  [{ts}] TRANSITION → {to}")
            if comment:
                print(f"    Comment: {comment}")
            print()

        elif etype == "comment":
            body = data.get("body", "")
            print(f"  [{ts}] COMMENT ({agent}):")
            print(_indent(body))
            print()

        elif etype == "agent_finished":
            print(f"  [{ts}] {agent} finished")
            print()

        elif etype == "agent_error":
            reason = data.get("reason", "?")
            print(f"  [{ts}] {agent} ERROR: {reason}")
            print()


def cmd_transcript(args):
    client, url = get_client(args)
    r = client.get(f"/api/v1/tickets/{args.ticket_id}")
    r.raise_for_status()
    ticket = r.json()

    events = _read_all_events(args.ticket_id)
    if not events:
        print(f"No event log found for {args.ticket_id}")
        print(f"  (looking in ~/.agentic-perf/logs/{args.ticket_id}.jsonl)")
        return

    if args.json:
        output = {
            "ticket_id": ticket["id"],
            "summary": ticket["summary"],
            "description": ticket["description"],
            "status": ticket["status"],
            "events": events,
        }
        if args.agent:
            output["events"] = [e for e in events if e.get("agent") == args.agent]
        json.dump(output, sys.stdout, indent=2, default=str)
        print()
        return

    _render_transcript(events, ticket, agent_filter=args.agent)


def cmd_health(args):
    client, url = get_client(args)
    r = client.get("/api/v1/health")
    r.raise_for_status()
    h = r.json()
    print(f"State store: {h['status']}")
    print(f"Total tickets: {h['total']}")
    for status, count in h.get("ticket_counts", {}).items():
        if count > 0:
            print(f"  {status}: {count}")


def main():
    show_disclaimer()

    parser = argparse.ArgumentParser(
        prog="agentic-perf",
        description="Agentic Performance Testing CLI",
    )
    parser.add_argument(
        "--store-url",
        default=DEFAULT_STORE_URL,
        help=f"State store URL (default: {DEFAULT_STORE_URL})",
    )
    sub = parser.add_subparsers(dest="command")

    p_submit = sub.add_parser("submit", help="Create a new test ticket")
    p_submit.add_argument("summary", help="Test request summary")
    p_submit.add_argument(
        "-d", "--description", help="Detailed description (defaults to summary)"
    )

    p_list = sub.add_parser("list", help="List tickets")
    p_list.add_argument("-s", "--status", help="Filter by status")

    p_show = sub.add_parser("show", help="Show ticket details")
    p_show.add_argument("ticket_id", help="Ticket ID")

    p_watch = sub.add_parser("watch", help="Watch ticket progress")
    p_watch.add_argument("ticket_id", help="Ticket ID")
    p_watch.add_argument(
        "-i", "--interval", type=float, default=3.0, help="Poll interval (seconds)"
    )
    p_watch.add_argument(
        "-f", "--follow", action="store_true", help="Keep watching after HITL pause"
    )
    p_watch.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show agent events (tool calls, LLM interactions)",
    )

    p_reply = sub.add_parser("reply", help="Reply to an agent's question")
    p_reply.add_argument("ticket_id", help="Ticket ID")
    p_reply.add_argument("message", help="Your response")
    p_reply.add_argument(
        "--abort",
        action="store_true",
        help="Abort the ticket after replying (skip to cleanup)",
    )

    p_approve = sub.add_parser("approve", help="Approve a pending command execution")
    p_approve.add_argument("ticket_id", help="Ticket ID")
    p_approve.add_argument(
        "--ticket",
        action="store_true",
        help="Approve this binary for the entire ticket",
    )

    p_deny = sub.add_parser("deny", help="Deny a pending command execution")
    p_deny.add_argument("ticket_id", help="Ticket ID")

    p_abort = sub.add_parser("abort", help="Abort a paused ticket and skip to cleanup")
    p_abort.add_argument("ticket_id", help="Ticket ID")
    p_abort.add_argument("reason", nargs="?", help="Reason for aborting (optional)")

    p_stop = sub.add_parser("stop", help="Stop a running agent on a ticket")
    p_stop.add_argument("ticket_id", help="Ticket ID")
    p_stop.add_argument(
        "--hard",
        action="store_true",
        help="Kill the agent immediately instead of waiting for it to finish",
    )

    p_stop_all = sub.add_parser(
        "stop-all",
        help="Stop all running agents across all active tickets",
    )
    p_stop_all.add_argument(
        "--hard",
        action="store_true",
        help="Kill all agents immediately",
    )
    p_stop_all.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt",
    )

    p_transcript = sub.add_parser(
        "transcript", help="Show full agent conversation transcript"
    )
    p_transcript.add_argument("ticket_id", help="Ticket ID")
    p_transcript.add_argument(
        "--json", action="store_true", help="Output raw events as JSON"
    )
    p_transcript.add_argument(
        "--agent", help="Filter to a single agent (e.g. triage-agent)"
    )

    sub.add_parser("health", help="Check state store health")

    p_cleanup = sub.add_parser("cleanup", help="Find/terminate orphaned AWS instances")
    p_cleanup.add_argument(
        "--older-than",
        type=float,
        metavar="HOURS",
        help="Only instances older than N hours",
    )
    p_cleanup.add_argument(
        "--terminate", action="store_true", help="Terminate matched instances"
    )
    p_cleanup.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    commands = {
        "submit": cmd_submit,
        "list": cmd_list,
        "show": cmd_show,
        "watch": cmd_watch,
        "reply": cmd_reply,
        "approve": cmd_approve,
        "deny": cmd_deny,
        "abort": cmd_abort,
        "stop": cmd_stop,
        "stop-all": cmd_stop_all,
        "transcript": cmd_transcript,
        "health": cmd_health,
        "cleanup": cmd_cleanup,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
