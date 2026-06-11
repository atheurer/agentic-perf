#!/usr/bin/env python3
"""Agentic Perf CLI — interact with tickets and agents."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_STORE_URL = "http://localhost:8090"


def get_client(args) -> tuple[httpx.Client, str]:
    url = args.store_url.rstrip("/")
    return httpx.Client(base_url=url, timeout=10.0), url


def cmd_submit(args):
    client, url = get_client(args)
    description = args.description or args.summary
    r = client.post("/api/v1/tickets", json={
        "summary": args.summary,
        "description": description,
    })
    r.raise_for_status()
    ticket = r.json()
    tid = ticket["id"]

    r = client.post(f"/api/v1/tickets/{tid}/transition", json={"status": "triage_pending"})
    r.raise_for_status()

    print(f"Created ticket: {tid}")
    print(f"Status: triage_pending")
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
            elif isinstance(val, str) and len(val) > 120:
                print(f"  {key}: {val[:120]}...")
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
        print(f"  Verbose mode: reading events from ~/.agentic-perf/logs/")
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
                    print(f"  [{time.strftime('%H:%M:%S')}] {c['author']}: {first_line}")
                    last_comment_count += 1

            if status in ("closed",):
                print()
                print("  Ticket closed.")
                break

            if status == "awaiting_customer_guidance":
                print()
                print("  >>> Agent is waiting for your input.")
                print(f"  >>> Use: agentic-perf reply {args.ticket_id} \"your response\"")
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

    r = client.post(f"/api/v1/tickets/{args.ticket_id}/comments", json={
        "author": "user",
        "body": args.message,
    })
    r.raise_for_status()

    previous = t.get("previous_status")
    if not previous:
        print("Warning: no previous_status recorded, cannot resume automatically.")
        return

    r = client.post(f"/api/v1/tickets/{args.ticket_id}/transition", json={
        "status": previous,
        "comment": "User responded, resuming pipeline",
    })
    r.raise_for_status()

    print(f"Reply added and ticket resumed to: {previous}")


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
        instances = [
            i for i in instances
            if i["LaunchTime"].timestamp() < cutoff
        ]

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
        ids = [i["InstanceId"] for i in instances]
        answer = input(f"\nTerminate {len(instances)} instance(s)? [y/N] ")
        if answer.lower() not in ("y", "yes"):
            print("Aborted.")
            return

    instance_ids = [i["InstanceId"] for i in instances]
    result = ec2.terminate_instances(InstanceIds=instance_ids)
    for i in result.get("TerminatingInstances", []):
        print(f"  {i['InstanceId']}: {i['PreviousState']['Name']} → {i['CurrentState']['Name']}")
    print(f"\nTerminated {len(instance_ids)} instance(s).")


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
    p_submit.add_argument("-d", "--description", help="Detailed description (defaults to summary)")

    p_list = sub.add_parser("list", help="List tickets")
    p_list.add_argument("-s", "--status", help="Filter by status")

    p_show = sub.add_parser("show", help="Show ticket details")
    p_show.add_argument("ticket_id", help="Ticket ID")

    p_watch = sub.add_parser("watch", help="Watch ticket progress")
    p_watch.add_argument("ticket_id", help="Ticket ID")
    p_watch.add_argument("-i", "--interval", type=float, default=3.0, help="Poll interval (seconds)")
    p_watch.add_argument("-f", "--follow", action="store_true", help="Keep watching after HITL pause")
    p_watch.add_argument("-v", "--verbose", action="store_true", help="Show agent events (tool calls, LLM interactions)")

    p_reply = sub.add_parser("reply", help="Reply to an agent's question")
    p_reply.add_argument("ticket_id", help="Ticket ID")
    p_reply.add_argument("message", help="Your response")

    p_health = sub.add_parser("health", help="Check state store health")

    p_cleanup = sub.add_parser("cleanup", help="Find/terminate orphaned AWS instances")
    p_cleanup.add_argument("--older-than", type=float, metavar="HOURS", help="Only instances older than N hours")
    p_cleanup.add_argument("--terminate", action="store_true", help="Terminate matched instances")
    p_cleanup.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

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
        "health": cmd_health,
        "cleanup": cmd_cleanup,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
