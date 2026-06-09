from __future__ import annotations

from typing import Any

import httpx


async def fetch_tickets_by_status(
    store_url: str, status: str
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{store_url}/api/v1/tickets",
            params={"status": status},
        )
        r.raise_for_status()
        return r.json()


async def fetch_changed_tickets(
    store_url: str, since_seq: int
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{store_url}/api/v1/tickets/since/{since_seq}")
        r.raise_for_status()
        return r.json()
