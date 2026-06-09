#!/usr/bin/env python3
"""Agentic Perf CLI — interact with tickets and agents."""

from __future__ import annotations

import argparse
import json
import sys
import time

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


def cmd_watch(args):
    client, url = get_client(args)
    last_comment_count = 0
    last_status = None

    print(f"Watching ticket {args.ticket_id} (Ctrl+C to stop)")
    print()

    try:
        while True:
            r = client.get(f"/api/v1/tickets/{args.ticket_id}")
            r.raise_for_status()
            t = r.json()

            status = t["status"]
            comments = t.get("comments", [])

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

    p_reply = sub.add_parser("reply", help="Reply to an agent's question")
    p_reply.add_argument("ticket_id", help="Ticket ID")
    p_reply.add_argument("message", help="Your response")

    p_health = sub.add_parser("health", help="Check state store health")

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
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
