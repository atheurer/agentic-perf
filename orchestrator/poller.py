from __future__ import annotations

import os
from typing import Any

import httpx


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def fetch_all_tickets(store_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        r = await client.get(
            f"{store_url}/api/v1/tickets",
            params={"exclude_status": "closed"},
        )
        r.raise_for_status()
        return r.json()


async def fetch_ticket_claim_reconciliation_page(
    store_url: str,
    *,
    after: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0, headers=_auth_headers()) as client:
        response = await client.get(
            f"{store_url}/api/v1/tickets/claim-reconciliation",
            params={"after": after, "limit": limit},
        )
        response.raise_for_status()
        page = response.json()
    tickets = page.get("tickets") if isinstance(page, dict) else None
    next_cursor = page.get("next_cursor") if isinstance(page, dict) else None
    read_errors = page.get("read_errors") if isinstance(page, dict) else None
    if (
        not isinstance(tickets, list)
        or not all(isinstance(ticket, dict) for ticket in tickets)
        or (next_cursor is not None and not isinstance(next_cursor, str))
        or not isinstance(read_errors, bool)
    ):
        raise ValueError("invalid claim reconciliation page")
    return {
        "tickets": tickets,
        "next_cursor": next_cursor,
        "read_errors": read_errors,
    }
